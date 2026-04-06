import hashlib
import os
import random
import secrets
import smtplib
import ssl
from datetime import datetime, UTC
from email.message import EmailMessage
from typing import Literal
import bcrypt
import httpx
import asyncpg
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, EmailStr

app = FastAPI(title="DayRing API", version="1.0.0")

API_VERSION = 1
DATABASE_URl = f"postgresql://atticus:{os.getenv('PSQL_PASSWORD')}@localhost:5432/dayring_api"

HF_API_KEY = os.getenv("HF_API_KEY")
HF_URL = "https://router.huggingface.co/v1/chat/completions"
HF_MODEL = os.getenv("HF_MODEL", "openai/gpt-oss-120b:novita")

SMTP_HOST:str = str(os.getenv("SMTP_HOST"))
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = str(os.getenv("SMTP_USER"))
SMTP_PASSWORD = str(os.getenv("SMTP_PASSWORD"))
SMTP_FROM_EMAIL = str(os.getenv("SMTP_FROM_EMAIL"))

if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD or not SMTP_FROM_EMAIL:
    raise RuntimeError("SMTP environment variables are not fully set")

if not HF_API_KEY:
    raise RuntimeError("HF_API_KEY is not set")

FEATURE_COSTS = {
    "plan_day": 2,
    "motivation": 1,
}

# Temporary in-memory store
credits = {
    "demo_user": 100
}


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feature: str = Field(..., min_length=1, max_length=50)
    messages: list[Message] = Field(..., min_length=1)


class CreditsResponse(BaseModel):
    user_id: str
    credits: int

class SignUpRequest(BaseModel):
    email: EmailStr
    password: str = Field(... ,max_length=72, min_length=8)

class ResetPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordValidationRequest():
    email: EmailStr
    code: str

def hash_reset_code(email: str, code: str) -> str:
    value = f"{email.lower().strip()}:{code}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def send_reset_email(to_email: str, code: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Your DayRing password reset code"
    msg["From"] = SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg.set_content(
        f"Your DayRing password reset code is: {code}\n"
        "This code expires in 15 minutes.\n"
        "If you did not request this code, you can ignore this email."
    )

    context = ssl.create_default_context()

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

@app.on_event("startup")
async def startup():
    app.state.db = await asyncpg.create_pool(DATABASE_URl)

@app.on_event("shutdown")
async def shutdown():
    await app.state.db.close()

@app.get("/health")
async def health():
    return {
        "ok": True,
        "model": HF_MODEL,
        "version": API_VERSION,
    }


@app.get(f"/api/v{API_VERSION}/credits", response_model=CreditsResponse)
async def get_credits(user_id: str = Query(..., min_length=1)):
    if user_id not in credits:
        raise HTTPException(status_code=404, detail="User not found")

    return CreditsResponse(user_id=user_id, credits=credits[user_id])


@app.post(f"/api/v{API_VERSION}/chat")
async def chat(
    req: ChatRequest,
    user_id: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
):
    # This is just a placeholder check for YOUR app auth, not HF auth
    if authorization is None:
        raise HTTPException(status_code=401, detail="Missing authorization header")

    if user_id not in credits:
        raise HTTPException(status_code=404, detail="User not found")

    cost = FEATURE_COSTS.get(req.feature, 1)

    if credits[user_id] < cost:
        raise HTTPException(status_code=402, detail="Out of credits")

    payload = {
        "model": HF_MODEL,
        "messages": [message.model_dump() for message in req.messages],
        "stream": False,
    }

    hf_headers = {
        "Authorization": f"Bearer {HF_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(HF_URL, json=payload, headers=hf_headers)

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Hugging Face error: {response.text}"
            )

        data = response.json()

        try:
            reply = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise HTTPException(
                status_code=502,
                detail="Unexpected response format from Hugging Face"
            )

        credits[user_id] -= cost

        return {
            "reply": reply,
            "credits_used": cost,
            "credits_remaining": credits[user_id],
            "model": HF_MODEL,
            "feature": req.feature,
        }

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream request timed out")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {str(e)}")

@app.post(f"/api/v{API_VERSION}/signup")
async def signup(req: SignUpRequest):
    email = req.email.lower().strip()

    async with app.state.db.acquire() as conn:
        existing_user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1",
            email
        )

    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered.")

    hashed_password = bcrypt.hashpw(
        req.password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")

    async with app.state.db.acquire() as conn:
        user = await conn.fetchrow(
            """
            INSERT INTO users (email, password_hash)
            VALUES($1, $2)
            RETURNING id
            """,
            email,
            hashed_password
        )
    return {
        "user_id": user["id"],
        "email": email,
        "message": "User successfully created!",
    }


def generate_six_digit_code():
    return str(random.randrange(100_000, 1_000_000))


@app.post(f"/api/v{API_VERSION}/password_reset/request")
async def request_password_reset(req: ResetPasswordRequest):
    email = req.email.lower().strip()

    async with app.state.db.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1",
             email
        )

    if not user:
        return {
            "message": "If that email exists, a reset code has been sent."
        }

    code = generate_six_digit_code()
    code_hash = hash_reset_code(email, code)
    expires_at = datetime.now(UTC) + datetime.timedelta(minutes=15)

    await conn.execute(
        "DELETE FROM password_reset_codes WHERE user_id = $1",
        user["id"]
    )

    await conn.execute(
        """
        INSERT INTO password_reset_codes (user_id, code_hash, expires_at)
        VALUES($1, $2, $3)
        """,
        user["id"],
        code_hash,
        expires_at
    )

    try:
        send_reset_email(email, code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(f"Failed to send email."))

    return {
        "message": "If that email exists, a reset code has been sent."
    }

@app.post(f"/api/v{API_VERSION}/password_reset/validate")
async def validate_password_reset():
    pass
