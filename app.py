"""
Bot de Finanzas por WhatsApp — Flask + Google Sheets
====================================================

Características
- Recibe mensajes de WhatsApp vía Webhook (WhatsApp Cloud API)
- Comandos:
  - gasto <monto> <categoria> <medio> "nota opcional"
  - ingreso <monto> <categoria> <medio> "nota opcional"
  - balance [mes opcional: 2025-09]
  - ayuda
- Guarda cada movimiento en una Google Sheet (misma estructura que la plantilla: Fecha, Tipo, Monto, Categoría, Medio, Nota)
- Responde con confirmación + balance del mes actual

Requisitos
- Cuenta en Meta Developers con WhatsApp Cloud API, número y token
- Google Cloud Service Account con acceso a la hoja
- Python 3.10+

Variables de entorno (ejemplo .env)
-----------------------------------
VERIFY_TOKEN=mi_token_verificacion
WHATSAPP_TOKEN=EAAG... (User Access Token o Permanent Token)
WHATSAPP_PHONE_NUMBER_ID=123456789012345
GOOGLE_SA_JSON={"type":"service_account", ...}
SHEET_ID=1abcDEFghIJkLmNoPqRstuVwxyz (ID de la Google Sheet)
PORT=8000
TZ=America/Mexico_City

Despliegue rápido (Render.com)
------------------------------
1) Crear nuevo servicio Web (Python) -> conectar repo con este archivo.
2) Build command:  pip install -r requirements.txt
   Start command:  gunicorn app:app --bind 0.0.0.0:$PORT
3) Configurar variables de entorno anteriores.
4) En Meta Developers > WhatsApp > Configuration: agregar la URL pública /webhook y el VERIFY_TOKEN.
5) Suscribir campos de webhook: messages.
6) Enviar un mensaje de prueba al número de WhatsApp y verificar.

requirements.txt sugerido
-------------------------
Flask==3.0.3
gunicorn==21.2.0
requests==2.32.3
gspread==6.1.2
google-auth==2.33.0
python-dotenv==1.0.1
pytz==2024.1

"""

from __future__ import annotations
import os
import re
import json
import pytz
import math
import logging
from datetime import datetime, date
from typing import Optional, Tuple, Dict, Any, List

import requests
from flask import Flask, request, jsonify

# --- Google Sheets ---
import gspread
from google.oauth2.service_account import Credentials

# --------------------- CONFIG ---------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("wa-fin-bot")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "verify_me")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
SHEET_ID = os.getenv("SHEET_ID", "")

# Timezone MX
TZ = os.getenv("TZ", "America/Mexico_City")
LOCAL_TZ = pytz.timezone(TZ)

# Google Service Account (como JSON en variable)
GOOGLE_SA_JSON = os.getenv("GOOGLE_SA_JSON", "")
if not GOOGLE_SA_JSON:
    logger.warning("GOOGLE_SA_JSON está vacío. Carga el JSON de la service account en la variable de entorno.")

# ------------------ APP & CLIENTS -----------------
app = Flask(__name__)

def get_gspread_client():
    try:
        sa_info = json.loads(GOOGLE_SA_JSON)
    except Exception as e:
        raise RuntimeError("GOOGLE_SA_JSON inválido. Coloca el JSON de la service account como string en la variable.") from e

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc

def open_sheet():
    if not SHEET_ID:
        raise RuntimeError("SHEET_ID no configurado")
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    # Asegura la hoja "Transacciones"
    try:
        ws = sh.worksheet("Transacciones")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet("Transacciones", rows=1000, cols=10)
        ws.update("A1:F1", [["Fecha","Tipo","Monto","Categoría","Medio","Nota"]])
    return sh, ws

# -------------------- HELPERS ---------------------
COMMANDS_HELP = (
    "Comandos disponibles:\n"
    "- gasto <monto> <categoria> <medio> \"nota opcional\"\n"
    "- ingreso <monto> <categoria> <medio> \"nota opcional\"\n"
    "- balance [YYYY-MM]\n"
    "Ejemplos:\n"
    "gasto 350 super tarjeta \"verduras y frutas\"\n"
    "ingreso 15000 sueldo transferencia \"pago quincena\"\n"
    "balance 2025-09"
)

RE_GASTO = re.compile(r"^\s*gasto\s+(?P<monto>[0-9]+(?:[\.,][0-9]{1,2})?)\s+(?P<cat>[^\s]+)\s+(?P<medio>[^\s]+)(?:\s+\"(?P<nota>[^\"]*)\")?\s*$", re.IGNORECASE)
RE_INGRESO = re.compile(r"^\s*ingreso\s+(?P<monto>[0-9]+(?:[\.,][0-9]{1,2})?)\s+(?P<cat>[^\s]+)\s+(?P<medio>[^\s]+)(?:\s+\"(?P<nota>[^\"]*)\")?\s*$", re.IGNORECASE)
RE_BALANCE = re.compile(r"^\s*balance(?:\s+(?P<ym>\d{4}-\d{2}))?\s*$", re.IGNORECASE)
RE_AYUDA = re.compile(r"^\s*ayuda\s*$", re.IGNORECASE)


def to_float(val: str) -> float:
    val = val.replace(",", ".")
    try:
        return round(float(val), 2)
    except Exception:
        raise ValueError("Monto inválido")


def now_local_iso() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_local_date() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def month_bounds(ym: Optional[str] = None) -> Tuple[str, str, str]:
    """Devuelve (YYYY-MM-01, YYYY-MM-31, etiqueta YYYY-MM). Si ym es None usa actual."""
    if ym:
        y, m = ym.split("-")
        y = int(y); m = int(m)
        first = date(y, m, 1)
    else:
        d = datetime.now(LOCAL_TZ).date()
        first = date(d.year, d.month, 1)
    # último día: truco sumando meses
    if first.month == 12:
        next_first = date(first.year+1, 1, 1)
    else:
        next_first = date(first.year, first.month+1, 1)
    last = next_first.replace(day=1) - (next_first - next_first)  # placeholder unused
    # para Sheets usaremos condición < primer_día_siguiente
    return first.strftime("%Y-%m-01"), next_first.strftime("%Y-%m-01"), first.strftime("%Y-%m")


def append_transaction(tipo: str, monto: float, categoria: str, medio: str, nota: str = "") -> None:
    _, ws = open_sheet()
    ws.append_row([today_local_date(), tipo.capitalize(), monto, categoria, medio, nota], value_input_option="USER_ENTERED")


def compute_balance(ym: Optional[str] = None) -> Dict[str, float]:
    sh, ws = open_sheet()
    data = ws.get_all_records()  # pequeña escala; para alto volumen se recomienda otro enfoque
    if ym is None:
        ym = datetime.now(LOCAL_TZ).strftime("%Y-%m")
    inc = 0.0
    gas = 0.0
    for row in data:
        f = str(row.get("Fecha", ""))
        if not f.startswith(ym):
            continue
        tipo = str(row.get("Tipo", "")).lower()
        try:
            monto = float(row.get("Monto", 0) or 0)
        except Exception:
            monto = 0.0
        if tipo == "ingreso":
            inc += monto
        elif tipo == "gasto":
            gas += monto
    return {"ingresos": round(inc, 2), "gastos": round(gas, 2), "balance": round(inc - gas, 2)}

# ---------------- WHATSAPP SEND/REPLY -------------

def wa_send_text(wa_id: str, text: str) -> None:
    if not (WHATSAPP_TOKEN and PHONE_NUMBER_ID):
        logger.error("Faltan WHATSAPP_TOKEN o WHATSAPP_PHONE_NUMBER_ID")
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "text",
        "text": {"body": text}
    }
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    if r.status_code >= 300:
        logger.error("Error enviando mensaje: %s %s", r.status_code, r.text)

# -------------------- WEBHOOKS --------------------

@app.get("/webhook")
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "forbidden", 403

@app.post("/webhook")
def inbound():
    data = request.get_json(force=True, silent=True) or {}
    try:
        changes = data.get("entry", [])[0].get("changes", [])[0]
        messages = changes.get("value", {}).get("messages", [])
        if not messages:
            return jsonify({"status": "ok"})
        msg = messages[0]
        wa_from = msg.get("from")  # phone
        text = (msg.get("text", {}) or {}).get("body", "").strip()
        logger.info("Msg from %s: %s", wa_from, text)

        # Routing
        if RE_AYUDA.match(text):
            wa_send_text(wa_from, COMMANDS_HELP)
            return jsonify({"status": "ok"})

        m = RE_GASTO.match(text)
        if m:
            monto = to_float(m.group("monto"))
            cat = m.group("cat")
            medio = m.group("medio")
            nota = m.group("nota") or ""
            append_transaction("Gasto", monto, cat, medio, nota)
            b = compute_balance()
            wa_send_text(wa_from, f"✅ Gasto registrado: ${monto:.2f} en {cat}\nBalance {datetime.now(LOCAL_TZ).strftime('%Y-%m')}: ${b['balance']:.2f} (Ing ${b['ingresos']:.2f} / Gas ${b['gastos']:.2f})")
            return jsonify({"status": "ok"})

        m = RE_INGRESO.match(text)
        if m:
            monto = to_float(m.group("monto"))
            cat = m.group("cat")
            medio = m.group("medio")
            nota = m.group("nota") or ""
            append_transaction("Ingreso", monto, cat, medio, nota)
            b = compute_balance()
            wa_send_text(wa_from, f"✅ Ingreso registrado: ${monto:.2f} ({cat})\nBalance {datetime.now(LOCAL_TZ).strftime('%Y-%m')}: ${b['balance']:.2f} (Ing ${b['ingresos']:.2f} / Gas ${b['gastos']:.2f})")
            return jsonify({"status": "ok"})

        m = RE_BALANCE.match(text)
        if m:
            ym = m.group("ym")
            b = compute_balance(ym)
            etiqueta = ym or datetime.now(LOCAL_TZ).strftime('%Y-%m')
            wa_send_text(wa_from, f"📊 Balance {etiqueta}: ${b['balance']:.2f}\nIngresos: ${b['ingresos']:.2f}\nGastos: ${b['gastos']:.2f}")
            return jsonify({"status": "ok"})

        # fallback
        wa_send_text(wa_from, "No entendí el comando. Escribe *ayuda* para ver ejemplos.")
        return jsonify({"status": "ok"})

    except Exception as e:
        logger.exception("Error en webhook: %s", e)
        return jsonify({"status": "error", "error": str(e)}), 200

# -------------------- MAIN ------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
