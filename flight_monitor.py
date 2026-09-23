#!/usr/bin/env python3
"""
Monitor de vuelos Buenos Aires (EZE) -> Nueva York (JFK)
==========================================================

Qué hace este script:
- Consulta la API "Flight Offers Search" de Amadeus (Self-Service, capa
  gratuita) para varias combinaciones de fecha de ida/vuelta entre el
  1 de agosto y el 30 de septiembre de 2027.
- Filtra por aerolíneas preferidas: American Airlines (AA) y sus
  principales socios de la alianza oneworld.
- Si encuentra un pasaje ida y vuelta por debajo del presupuesto
  definido, envía una alerta a un chat/canal de Telegram vía un bot.

Todas las credenciales se leen desde variables de entorno (nunca se
escriben en el código), para poder usarlas de forma segura tanto en
tu máquina como en GitHub Actions (ver .github/workflows/monitor.yml).

Variables de entorno requeridas:
    AMADEUS_API_KEY        -> API Key de Amadeus for Developers
    AMADEUS_API_SECRET     -> API Secret de Amadeus for Developers
    TELEGRAM_BOT_TOKEN     -> Token del bot de Telegram (vía BotFather)
    TELEGRAM_CHAT_ID       -> ID del chat/canal donde se enviarán las alertas

Variables de entorno opcionales (tienen valores por defecto):
    ORIGIN                 -> Código IATA de origen (default: EZE)
    DESTINATION            -> Código IATA de destino (default: JFK)
    BUDGET_USD             -> Presupuesto máximo en USD (default: 950)
    PREFERRED_AIRLINES     -> Lista de códigos de aerolínea separados por coma
"""

import os
import sys
import time
import logging
from datetime import date, timedelta

import requests
from amadeus import Client, ResponseError

# ----------------------------------------------------------------------
# CONFIGURACIÓN
# ----------------------------------------------------------------------

ORIGIN = os.environ.get("ORIGIN", "EZE")
DESTINATION = os.environ.get("DESTINATION", "JFK")

# Rango de fechas del viaje (podés cambiarlo o pasarlo por variable de
# entorno si querés reutilizar el script para otras búsquedas)
SEARCH_START = date.fromisoformat(os.environ.get("SEARCH_START", "2027-08-01"))
SEARCH_END = date.fromisoformat(os.environ.get("SEARCH_END", "2027-09-30"))

# Duraciones de estadía (en días) que se van a probar. Buscar TODAS las
# combinaciones de ida/vuelta posibles en un rango de 2 meses sería
# larguísimo y agotaría la cuota gratuita de la API en un solo día, así
# que probamos duraciones "típicas" de viaje.
STAY_DURATIONS_DAYS = [7, 10, 14, 21]

# Cada cuántos días se prueba una nueva fecha de salida. Con step=3 se
# hacen ~20 fechas de salida x 4 duraciones = ~80 búsquedas por corrida,
# lo cual entra cómodo en la cuota gratuita mensual de Amadeus (ver
# instrucciones más abajo). Si querés más cobertura, bajalo a 1 o 2,
# pero vas a consumir la cuota gratuita más rápido.
DEPARTURE_STEP_DAYS = int(os.environ.get("DEPARTURE_STEP_DAYS", "3"))

BUDGET_USD = float(os.environ.get("BUDGET_USD", "950"))

# American Airlines + principales socios de oneworld que también
# cubren la ruta EZE-JFK (directo o con escala): AA, BA, IB, QR, AY,
# LA (LATAM), JL, QF, CX, RJ, MH, UL, AT, FJ.
PREFERRED_AIRLINES = os.environ.get(
    "PREFERRED_AIRLINES",
    "AA,BA,IB,QR,AY,LA,JL,QF,CX,RJ,MH,UL,AT,FJ",
).split(",")

CURRENCY = "USD"
MAX_RESULTS_PER_SEARCH = 5   # ofertas devueltas por cada búsqueda
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

def daterange_departures(start, end, step_days):
    """Genera fechas de salida entre start y end, cada step_days días."""
    d = start
    while d <= end:
        yield d
        d += timedelta(days=step_days)


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


def search_offers(departure_date, return_date):
    """Consulta Flight Offers Search para una combinación de fechas dada."""
    try:
        response = amadeus.shopping.flight_offers_search.get(
            originLocationCode=ORIGIN,
            destinationLocationCode=DESTINATION,
            departureDate=departure_date.isoformat(),
            returnDate=return_date.isoformat(),
            adults=1,
            currencyCode=CURRENCY,
            includedAirlineCodes=",".join(PREFERRED_AIRLINES),
            max=MAX_RESULTS_PER_SEARCH,
            nonStop="false",
        )
        return response.data
    except ResponseError as error:
        log.warning(
            f"Sin resultados/error para salida {departure_date} "
            f"/ regreso {return_date}: {error}"
        )
        return []


# ----------------------------------------------------------------------
# LÓGICA PRINCIPAL
# ----------------------------------------------------------------------

def run():
    log.info(
        f"Buscando vuelos {ORIGIN}→{DESTINATION} entre {SEARCH_START} y "
        f"{SEARCH_END} | presupuesto: {BUDGET_USD} {CURRENCY} | "
        f"aerolíneas: {','.join(PREFERRED_AIRLINES)}"
    )

    best_offers = []
    searches_done = 0

    for dep in daterange_departures(SEARCH_START, SEARCH_END, DEPARTURE_STEP_DAYS):
        for stay in STAY_DURATIONS_DAYS:
            ret = dep + timedelta(days=stay)
            if ret > SEARCH_END:
                continue

            offers = search_offers(dep, ret)
            searches_done += 1
            time.sleep(REQUEST_DELAY_SECONDS)

            for offer in offers:
                try:
                    price, currency, ida, vuelta = format_offer(offer)
                except (KeyError, ValueError, IndexError):
                    continue

                if price < BUDGET_USD:
                    best_offers.append(
                        {
                            "price": price,
                            "currency": currency,
                            "departure": dep.isoformat(),
                            "return": ret.isoformat(),
                            "ida": ida,
                            "vuelta": vuelta,
                        }
                    )

    log.info(f"Búsquedas realizadas: {searches_done}. Ofertas bajo presupuesto: {len(best_offers)}.")

    if not best_offers:
        log.info("No se encontraron ofertas por debajo del presupuesto en esta corrida.")
        return

    best_offers.sort(key=lambda o: o["price"])
    top_offers = best_offers[:5]  # no saturar el mensaje de Telegram

    lines = [
        f"✈️ <b>Ofertas {ORIGIN}→{DESTINATION}</b> por debajo de "
        f"{BUDGET_USD:.0f} {CURRENCY}\n"
    ]
    for o in top_offers:
        lines.append(
            f"💰 <b>{o['price']:.2f} {o['currency']}</b>\n"
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
