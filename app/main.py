import hashlib
import os
import random
import secrets
import smtplib
import ssl
from contextlib import asynccontextmanager
from datetime import datetime, UTC, timedelta
from email.message import EmailMessage
from typing import Literal
import bcrypt
import httpx
import asyncpg
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict, EmailStr

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(DATABASE_URL, ssl=False)
    yield
    await app.state.db.close()
app = FastAPI(title="DayRing API", version="1.0.0", lifespan=lifespan)

API_VERSION = 1
DATABASE_URL = f"postgresql://atticus:{os.getenv('PSQL_PASSWORD')}@10.0.0.235:5432/dayring_api"
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
    username: str = Field(..., max_length=20, min_length=3)
    password: str = Field(... ,max_length=72, min_length=8)

class LogInRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)

class ResetPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordValidationRequest(BaseModel):
    email: EmailStr
    code: str

class ResetPasswordConfirmationRequest(BaseModel):
    password: str = Field(..., max_length=72, min_length=8)
    authorization: str

def hash_reset_code(email: str, code: str) -> str:
    value = f"{email.lower().strip()}:{code}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def generate_access_token() -> str:
    return secrets.token_urlsafe(32)

def hash_access_token(token:str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

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

def generate_six_digit_code():
    return str(random.randrange(100_000, 1_000_000))


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

@app.post(f"/api/v{API_VERSION}/auth/signup")
async def signup(req: SignUpRequest):
    email = req.email.lower().strip()
    username = req.username.lower().strip()

    async with app.state.db.acquire() as conn:
        existing_user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1 OR username = $2",
            email,
            username
        )

        if existing_user:
            raise HTTPException(status_code=409, detail="Email or username already registered.")

        hashed_password = bcrypt.hashpw(
        req.password.encode("utf-8"),
        bcrypt.gensalt(),
        ).decode("utf-8")

        user = await conn.fetchrow(
            """
            INSERT INTO users (email,username,password_hash)
            VALUES($1, $2, $3)
            RETURNING id
            """,
            email,
            username,
            hashed_password
        )

        access_token = generate_access_token()
        access_token_hash = hash_access_token(access_token)
        expires_at = datetime.now(UTC) + timedelta(days=7)

        await conn.execute(
            """
            INSERT INTO user_sessions (user_id, access_token, expires_at)
            values ($1, $2, $3)
            """,
            user["id"], access_token_hash, expires_at
        )

    return {
        "user_id": user["id"],
        "email": email,
        "username": username,
        "message": "User successfully created!",
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 604800
    }


@app.post(f"/api/v{API_VERSION}/auth/login")
async def login(req: LogInRequest):
    email = req.email.lower().strip()

    async with app.state.db.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, email, username, password_hash
            FROM users
            WHERE email = $1
            """,
            email
        )

        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        password_ok = bcrypt.checkpw(password=req.password.encode("utf-8"), hashed_password=user["password_hash"])

        if not password_ok:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        access_token = generate_access_token()
        token_hash = hash_access_token(access_token)
        expires_at = datetime.now(UTC) + timedelta(days=7)

        await conn.execute(
            """
            INSERT INTO user_sessions(user_id, token_hash, expires_at)
            VALUES($1, $2, $3)
            """,
            user["id"], token_hash, expires_at
        )
    return {
        "Message": "Login successful!",
        "user_id": user["id"],
        "user_email": user["email"],
        "username": user["username"],
        "access_token": access_token,
        "expires_at": expires_at
    }

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
        expires_at = datetime.now(UTC) + timedelta(minutes=15)

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
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to send email.")

    return {
        "message": "If that email exists, a reset code has been sent."
    }

@app.post(f"/api/v{API_VERSION}/password_reset/validate")
async def validate_password_reset(req: ResetPasswordValidationRequest):
    email = req.email.lower().strip()
    code = req.code.strip()

    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code = 400, detail = "Invalid code format.")

    code_hash = hash_reset_code(email, code)
    reset_token = generate_access_token()
    reset_token_hash = hash_access_token(token=reset_token)
    session_expires_at = datetime.now(UTC) + timedelta(minutes = 20)

    async with app.state.db.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE email = $1",
            email
        )

        if not user:
            raise HTTPException(status_code= 400, detail="Invalid or expired reset code.")

        reset_code_row = await conn.fetchrow(
            """
            SELECT id
            FROM password_reset_codes
            WHERE user_id = $1
                AND code_hash = $2
                AND expires_at > now()
            ORDER BY expires_at DESC
            LIMIT 1
            """,
            user["id"],
            code_hash
        )

        if not reset_code_row:
            raise HTTPException(status_code=400, detail="Invalid or expired reset code.")

        # Consumes the code sent to the users email to prevent code resuing.
        await conn.execute(
            """
            DELETE FROM password_reset_codes WHERE id = $1
            """,
            reset_code_row["id"]
        )

        # Delete old sessions for this user.
        await conn.execute(
            """
            DELETE FROM password_reset_sessions WHERE user_id = $1
            """,
            user["id"]
        )

        await conn.execute(
            """
            INSERT INTO password_reset_sessions (user_id, token_hash, expires_at)
            VALUES($1, $2, $3)
            """,
            user["id"],
            reset_token_hash,
            session_expires_at,
        )

    return {
        "message": "Reset code is valid",
        "reset_token": reset_token,
        "token_type": "bearer",
        "expires_in": 1200
    }

@app.post(f"/api/v{API_VERSION}/password_reset/confirm")
async def confirm_password_reset(
        req: ResetPasswordConfirmationRequest,
):
    if req.authorization is None:
        raise HTTPException(status_code=400, detail="Expired or invalid authorization token.")

    raw_token = req.authorization

    token_hash = hash_access_token(raw_token)
    new_password_hash = bcrypt.hashpw(req.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    async with app.state.db.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            SELECT id, user_id
            FROM password_reset_sessions
            WHERE token_hash = $1
                AND expires_at > now()
            ORDER BY created_at DESC
            LIMIT 1
            """,
            token_hash
        )
        if not session_row:
            raise HTTPException(status_code=400, detail="Expired or invalid reset token.")

        # Updates the users password
        await conn.execute(
            """
            UPDATE users
            SET password_hash = $1
            WHERE id = $2
            """,
            new_password_hash,
            session_row["user_id"]
        )

        # Invalidates previous reset token
        await conn.execute(
            """
            DELETE FROM password_reset_sessions WHERE id = $1
            """,
            session_row["id"]
        )

    return {
        "message": "Password was reset successfully",
    }
