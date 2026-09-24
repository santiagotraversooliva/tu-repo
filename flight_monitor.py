#!/usr/bin/env python3
"""
Monitor de vuelos Buenos Aires (EZE) -> Nueva York (JFK / EWR)
==================================================================

Qué hace este script:
- Consulta la API "Flight Offers Search" de Amadeus (Self-Service, capa
  gratuita) para un viaje con fechas FIJAS: salida 17/09/2027 y
  regreso 25/09/2027 o, si no hay buenas opciones, 02/10/2027.
- Busca tanto en JFK como en EWR como aeropuertos de llegada/salida en
  Nueva York.
- Filtra por aerolíneas preferidas: American Airlines, United, Delta,
  LATAM, Avianca y Copa. Sin restricción de escalas. Cabina económica.
- Prioriza (no descarta) los vuelos que respetan la preferencia
  horaria: ida nocturna desde Buenos Aires con llegada a la mañana
  siguiente en Nueva York, y vuelta saliendo por la tarde/noche desde
  Nueva York con llegada a la mañana siguiente en Buenos Aires.
- Si encuentra un pasaje ida y vuelta por debajo del presupuesto
  definido, envía una alerta a un chat/canal de Telegram vía un bot.

Todas las credenciales se leen desde variables de entorno (nunca se
escriben en el código), para poder usarlas de forma segura tanto en
tu máquina como en GitHub Actions (ver .github/workflows/monitor.yml).
No hace falta tocar el workflow: las variables nuevas tienen valores
por defecto que ya reflejan lo pedido.

Variables de entorno requeridas:
    AMADEUS_API_KEY        -> API Key de Amadeus for Developers
    AMADEUS_API_SECRET     -> API Secret de Amadeus for Developers
    TELEGRAM_BOT_TOKEN     -> Token del bot de Telegram (vía BotFather)
    TELEGRAM_CHAT_ID       -> ID del chat/canal donde se enviarán las alertas

Variables de entorno opcionales (tienen valores por defecto):
    ORIGIN                 -> Código IATA de origen (default: EZE)
    DESTINATIONS           -> Códigos IATA de destino separados por coma
                               (default: JFK,EWR)
    DEPARTURE_DATE          -> Fecha de ida fija (default: 2027-09-17)
    RETURN_DATE_OPTIONS     -> Fechas de vuelta candidatas, separadas por
                               coma (default: 2027-09-25,2027-10-02)
    BUDGET_USD             -> Presupuesto máximo en USD (default: 950)
    PREFERRED_AIRLINES     -> Lista de códigos de aerolínea separados por coma
"""

import os
import sys
import time
import logging
from datetime import date, datetime

import requests
from amadeus import Client, ResponseError

# ----------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------

ORIGIN = os.environ.get("ORIGIN", "EZE")

# Nueva York se cubre con JFK y EWR (Newark). El script busca en los dos
# y se queda con lo mejor de cada uno.
DESTINATIONS = os.environ.get("DESTINATIONS", "JFK,EWR").split(",")

# Fechas FIJAS del viaje. La ida es única; la vuelta tiene dos fechas
# candidatas y se buscan ambas en cada corrida.
DEPARTURE_DATE = date.fromisoformat(os.environ.get("DEPARTURE_DATE", "2027-09-17"))
RETURN_DATE_OPTIONS = [
    date.fromisoformat(d.strip())
    for d in os.environ.get("RETURN_DATE_OPTIONS", "2027-09-25,2027-10-02").split(",")
]

BUDGET_USD = float(os.environ.get("BUDGET_USD", "950"))

# American Airlines + United, Delta, LATAM, Avianca y Copa (todas
# operan o tienen código compartido en la ruta EZE/AEP-JFK/EWR).
PREFERRED_AIRLINES = os.environ.get(
    "PREFERRED_AIRLINES",
    "AA,UA,DL,LA,AV,CM",
).split(",")

CABIN_CLASS = os.environ.get("CABIN_CLASS", "ECONOMY")  # cabina económica

# --- Preferencias horarias --------------------------------------------
# Ida (17/09): salida "nocturna" desde EZE y llegada "a la mañana
# siguiente" a Nueva York. Horas en formato 24hs, hora local de cada
# aeropuerto (así vienen los datos de Amadeus).
OUTBOUND_DEPARTURE_HOUR_RANGE = (19, 23)   # sale entre las 19:00 y las 23:59
OUTBOUND_ARRIVAL_HOUR_RANGE = (4, 12)      # llega entre las 04:00 y las 11:59
OUTBOUND_REQUIRE_NEXT_DAY_ARRIVAL = True   # debe llegar al día siguiente

# Vuelta (25/09 o 02/10): salida por la tarde/noche desde Nueva York y
# llegada a la mañana siguiente a Buenos Aires.
RETURN_DEPARTURE_HOUR_RANGE = (15, 23)     # sale entre las 15:00 y las 23:59
RETURN_ARRIVAL_HOUR_RANGE = (4, 12)        # llega entre las 04:00 y las 11:59
RETURN_REQUIRE_NEXT_DAY_ARRIVAL = True     # debe llegar al día siguiente

CURRENCY = "USD"
MAX_RESULTS_PER_SEARCH = 10  # ofertas devueltas por cada búsqueda
REQUEST_DELAY_SECONDS = 0.6  # pausa entre llamadas para cuidar el rate limit

# ----------------------------------------------------------------------
# CREDENCIALES (obligatorias, se leen de variables de entorno)
# ----------------------------------------------------------------------

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: falta la variable de entorno '{name}'.", file=sys.stderr)
        sys.exit(1)
    return value


AMADEUS_API_KEY = _require_env("AMADEUS_API_KEY")
AMADEUS_API_SECRET = _require_env("AMADEUS_API_SECRET")
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("flight_monitor")

# Cliente de Amadeus. Por defecto usa el entorno de pruebas ("test"),
# que es el que tiene cuota gratuita. Ver instrucciones más abajo.
amadeus = Client(client_id=AMADEUS_API_KEY, client_secret=AMADEUS_API_SECRET)


# ----------------------------------------------------------------------
# FUNCIONES AUXILIARES
# ----------------------------------------------------------------------

def _parse_dt(iso_str):
    """Convierte el string de fecha/hora local que devuelve Amadeus en datetime."""
    return datetime.fromisoformat(iso_str)


def matches_time_preference(itinerary, dep_range, arr_range, require_next_day):
    """
    Evalúa si un itinerario (un tramo: ida o vuelta) respeta la ventana
    horaria preferida. Devuelve True/False; no descarta ofertas que no
    matcheen, solo se usa para ordenarlas (las preferidas primero).
    """
    segments = itinerary["segments"]
    dep = _parse_dt(segments[0]["departure"]["at"])
    arr = _parse_dt(segments[-1]["arrival"]["at"])

    dep_ok = dep_range[0] <= dep.hour <= dep_range[1]
    arr_ok = arr_range[0] <= arr.hour <= arr_range[1]
    day_ok = (arr.date() > dep.date()) if require_next_day else True

    return dep_ok and arr_ok and day_ok


def send_telegram_alert(text):
    """Envía un mensaje de texto (HTML) al chat/canal de Telegram configurado."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        log.info("Alerta enviada a Telegram correctamente.")
    except requests.RequestException as e:
        log.error(f"Error enviando mensaje a Telegram: {e}")


def format_offer(offer):
    """Extrae precio y un resumen legible de ida/vuelta de una oferta de Amadeus."""
    price = float(offer["price"]["grandTotal"])
    currency = offer["price"]["currency"]
    itineraries = offer["itineraries"]

    def leg_summary(itinerary):
        segments = itinerary["segments"]
        first, last = segments[0], segments[-1]
        stops = len(segments) - 1
        stops_txt = "directo" if stops == 0 else f"{stops} escala(s)"
        return (
            f"{first['carrierCode']}{first['number']} | "
            f"{first['departure']['at']} → {last['arrival']['at']} | "
            f"{stops_txt}"
        )

    ida = leg_summary(itineraries[0])
    vuelta = leg_summary(itineraries[1]) if len(itineraries) > 1 else "N/A"
    return price, currency, ida, vuelta


def search_offers(destination, departure_date, return_date):
    """
    Consulta Flight Offers Search para una combinación de destino
    (JFK o EWR) y fechas de ida/vuelta. No se restringe cantidad de
    escalas (se omite el parámetro nonStop) y se pide cabina económica.
    """
    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=ORIGIN,
            destinationLocationCode=destination,
            departureDate=departure_date.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            currencyCode=CURRENCY,
            includedAirlineCodes=",".join(PREFERRED_AIRLINES),
            travelClass=CABIN_CLASS,
            max=MAX_RESULTS_PER_SEARCH,
        )
        return response.data
    except ResponseError as error:
        log.warning(
            f"Sin resultados/error para {ORIGIN}->{destination} "
            f"salida {departure_date} / regreso {return_date}: {error}"
        )
        return []


# ----------------------------------------------------------------------
# LÓGICA PRINCIPAL
# ----------------------------------------------------------------------

def run():
    log.info(
        f"Buscando vuelos {ORIGIN}→{'/'.join(DESTINATIONS)} | "
        f"ida fija: {DEPARTURE_DATE} | vueltas candidatas: "
        f"{', '.join(d.isoformat() for d in RETURN_DATE_OPTIONS)} | "
        f"presupuesto: {BUDGET_USD} {CURRENCY} | "
        f"aerolíneas: {','.join(PREFERRED_AIRLINES)} | cabina: {CABIN_CLASS}"
    )

    best_offers = []
    searches_done = 0

    for destination in DESTINATIONS:
        for ret in RETURN_DATE_OPTIONS:
            offers = search_offers(destination, DEPARTURE_DATE, ret)
            searches_done += 1
            time.sleep(REQUEST_DELAY_SECONDS)

            for offer in offers:
                try:
                    price, currency, ida, vuelta = format_offer(offer)
                    itineraries = offer["itineraries"]
                except (KeyError, ValueError, IndexError):
                    continue

                if price >= BUDGET_USD:
                    continue

                outbound_match = matches_time_preference(
                    itineraries[0],
                    OUTBOUND_DEPARTURE_HOUR_RANGE,
                    OUTBOUND_ARRIVAL_HOUR_RANGE,
                    OUTBOUND_REQUIRE_NEXT_DAY_ARRIVAL,
                )
                return_match = (
                    matches_time_preference(
                        itineraries[1],
                        RETURN_DEPARTURE_HOUR_RANGE,
                        RETURN_ARRIVAL_HOUR_RANGE,
                        RETURN_REQUIRE_NEXT_DAY_ARRIVAL,
                    )
                    if len(itineraries) > 1
                    else False
                )

                best_offers.append(
                    {
                        "price": price,
                        "currency": currency,
                        "destination": destination,
                        "departure": DEPARTURE_DATE.isoformat(),
                        "return": ret.isoformat(),
                        "ida": ida,
                        "vuelta": vuelta,
                        "outbound_match": outbound_match,
                        "return_match": return_match,
                        "schedule_score": int(outbound_match) + int(return_match),
                    }
                )

    log.info(
        f"Búsquedas realizadas: {searches_done}. "
        f"Ofertas bajo presupuesto: {len(best_offers)}."
    )

    if not best_offers:
        log.info("No se encontraron ofertas por debajo del presupuesto en esta corrida.")
        return

    # Orden: primero las que más coinciden con el horario preferido
    # (ida nocturna + vuelta tarde/noche, ambas llegando a la mañana
    # siguiente), y dentro de cada grupo, de más barata a más cara.
    best_offers.sort(key=lambda o: (-o["schedule_score"], o["price"]))
    top_offers = best_offers[:5]  # no saturar el mensaje de Telegram

    lines = [
        f"✈️ <b>Ofertas {ORIGIN}→NYC (JFK/EWR)</b> por debajo de "
        f"{BUDGET_USD:.0f} {CURRENCY}\n"
        f"Ida fija: {DEPARTURE_DATE.isoformat()}\n"
    ]
    for o in top_offers:
        schedule_tag = (
            "🌙✅ Horario ideal (ida nocturna + vuelta tarde/noche)"
            if o["schedule_score"] == 2
            else "🌙〜 Parcialmente ideal"
            if o["schedule_score"] == 1
            else "⏱️ No coincide con horario preferido"
        )
        lines.append(
            f"💰 <b>{o['price']:.2f} {o['currency']}</b> — destino {o['destination']}\n"
            f"{schedule_tag}\n"
            f"📅 Ida: {o['departure']} — {o['ida']}\n"
            f"📅 Vuelta: {o['return']} — {o['vuelta']}\n"
        )

    send_telegram_alert("\n".join(lines))


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log.exception("Error inesperado durante la ejecución del monitor.")
        sys.exit(1)
