from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Betsy")

app.mount("/static", StaticFiles(directory="app/static", html=True), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# TODO: protocol/turn/safety routes
