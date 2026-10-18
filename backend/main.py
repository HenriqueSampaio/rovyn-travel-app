import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

from backend.routers import auth_routes, bookings, payments, places, social, trips
from backend.routers.social import MEDIA_DIR
from backend.services import reconciler
from backend.services.payment_service import refuse_live_keys

# Only provably-test credentials (sk_test_ / sand_ / duffel_test_ / empty) are
# accepted unless ROVYN_LIVE_MONEY=1 — see docs/MOR-RUNBOOK.md.
refuse_live_keys()

app = FastAPI(title="Rovyn Travel Planner API")

# The planning board's options JSON grows to hundreds of KB once live details
# are cached inside it, and the board polls it every few seconds — compress
# everything sizeable.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Hosted deployments list their frontend origin(s) in ALLOWED_ORIGINS,
# comma-separated; localhost stays allowed for development.
_extra_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", *_extra_origins],
    # localhost for development, plus private LAN IPs so a phone on the same
    # Wi-Fi can hit the dev servers (http://192.168.x.x:5173 etc.).
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1|192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router, prefix="/auth", tags=["auth"])
app.include_router(trips.router, prefix="/trips", tags=["trips"])
app.include_router(bookings.router, prefix="/trips", tags=["bookings"])
app.include_router(payments.router, prefix="/trips", tags=["payments"])
app.include_router(payments.webhooks_router, prefix="/webhooks", tags=["webhooks"])
app.include_router(places.router, prefix="/places", tags=["places"])
app.include_router(social.router, prefix="/social", tags=["social"])

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


@app.on_event("startup")
def _start_reconciler() -> None:
    # Resumes settlements a dead process left half-done; ROVYN_RECONCILER=0 disables it.
    reconciler.start()


@app.get("/health")
def health():
    return {"status": "ok"}
