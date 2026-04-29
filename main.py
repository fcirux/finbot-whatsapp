import os
import json
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="FinBot - WhatsApp Finance Assistant")

WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

user_data: dict = {}

def get_user_state(phone: str) -> dict:
    if phone not in user_data:
        user_data[phone] = {
            "transactions": [],
            "budget": {
                "Comida": 300, "Transporte": 150, "Ocio": 200,
                "Salud": 100, "Hogar": 500, "Trabajo": 0, "Otros": 100
            },
            "history": []
        }
    return user_data[phone]

async def ask_claude(user_message: str, state: dict) -> dict:
    from datetime import datetime
    now = datetime.now()
    transactions = state["transactions"]
    budget = state["budget"]
    history = state["history"]

    monthly = [t for t in transactions
               if datetime.fromisoformat(t["date"]).month == now.month
               and datetime.fromisoformat(t["date"]).year == now.year]

    expenses = [t for t in monthly if t["type"] == "expense"]
    incomes  = [t for t in monthly if t["type"] == "income"]
    total_expenses = sum(t["amount"] for t in expenses)
    total_income   = sum(t["amount"] for t in incomes)

    by_category = {}
    for cat in budget:
        by_category[cat] = sum(t["amount"] for t in expenses if t["category"] == cat)

    system_prompt = f"""Eres FinBot, un asistente financiero personal por WhatsApp en espanol.
Eres conciso, amigable y usas emojis con moderacion.

ESTADO FINANCIERO (mes actual):
- Ingresos: {total_income:.2f}
- Gastos: {total_expenses:.2f}
- Balance: {total_income - total_expenses:.2f}
- Gastos por categoria: {json.dumps(by_category)}
- Presupuestos: {json.dumps(budget)}
- Ultimas 5 transacciones: {json.dumps(transactions[-5:])}

Responde SOLO con JSON valido, sin markdown:
{{
  "message": "respuesta para WhatsApp (max 300 chars)",
  "action": "add_transaction o show_summary o set_budget o none",
  "transaction": {{
    "type": "expense o income",
    "amount": 0,
    "description": "descripcion",
    "category": "Comida o Transporte o Ocio o Salud o Hogar o Trabajo o Otros"
  }},
  "budget_update": null,
  "alert": null
}}"""

    messages = []
    for h in history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": messages,
            }
        )
        data = response.json()

    raw = data["content"][0]["text"]
    try:
        return json.loads(raw.strip())
    except Exception:
        return {"message": raw[:300], "action": "none"}

async def send_whatsapp_message(to: str, text: str):
    url = f"https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient(timeout=15) as client:
        await client.post(url, headers=headers, json=payload)

def build_summary_text(state: dict) -> str:
    from datetime import datetime
    now = datetime.now()
    transactions = state["transactions"]
    budget = state["budget"]

    monthly  = [t for t in transactions
                if datetime.fromisoformat(t["date"]).month == now.month]
    expenses = [t for t in monthly if t["type"] == "expense"]
    incomes  = [t for t in monthly if t["type"] == "income"]
    total_exp = sum(t["amount"] for t in expenses)
    total_inc = sum(t["amount"] for t in incomes)
    balance   = total_inc - total_exp

    lines = [
        f"Resumen de {now.strftime('%B %Y')}",
        f"Ingresos:  {total_inc:.2f}",
        f"Gastos:    {total_exp:.2f}",
        f"Balance:   {balance:.2f}",
        "",
        "Por categoria:",
    ]

    for cat, limit in budget.items():
        spent = sum(t["amount"] for t in expenses if t["category"] == cat)
        if spent > 0:
            bar = f"{spent:.0f}"
            if limit > 0:
                pct = int((spent / limit) * 100)
                warn = " ALERTA" if pct >= 80 else ""
                bar += f"/{limit:.0f} ({pct}%){warn}"
            lines.append(f"{cat}: {bar}")

    return "\n".join(lines)

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=403, detail="Token invalido")

@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()

    try:
        entry   = body["entry"][0]
        changes = entry["changes"][0]
        value   = changes["value"]

        if "messages" not in value:
            return {"status": "ok"}

        message     = value["messages"][0]
        from_number = message["from"]

        if message["type"] != "text":
            await send_whatsapp_message(from_number, "Solo entiendo mensajes de texto por ahora.")
            return {"status": "ok"}

        user_text = message["text"]["body"].strip()
        state = get_user_state(from_number)
        result = await ask_claude(user_text, state)

        state["history"].append({"role": "user",      "content": user_text})
        state["history"].append({"role": "assistant", "content": result.get("message", "")})
        if len(state["history"]) > 20:
            state["history"] = state["history"][-20:]

        action = result.get("action", "none")

        if action == "add_transaction" and result.get("transaction"):
            from datetime import datetime
            tx = result["transaction"]
            tx["date"] = datetime.now().isoformat()
            tx["id"]   = len(state["transactions"]) + 1
            state["transactions"].append(tx)

        if action == "set_budget" and result.get("budget_update"):
            bu = result["budget_update"]
            if bu and bu.get("category") in state["budget"]:
                state["budget"][bu["category"]] = bu["amount"]

        reply = result.get("message", "No entendi, podes repetirlo?")
        await send_whatsapp_message(from_number, reply)

        if action == "show_summary":
            await send_whatsapp_message(from_number, build_summary_text(state))

        if result.get("alert"):
            await send_whatsapp_message(from_number, f"ALERTA: {result['alert']}")

    except (KeyError, IndexError) as e:
        print(f"Error procesando mensaje: {e}")

    return {"status": "ok"}

@app.get("/")
async def root():
    return {"status": "FinBot activo", "version": "1.0"}

@app.get("/health")
async def health():
    return {"status": "ok"}
