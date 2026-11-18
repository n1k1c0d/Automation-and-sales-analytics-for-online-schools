from fastapi import FastAPI

from app.api.routes.health import router as health_router

app = FastAPI(title="EduFlow AI")
app.include_router(health_router)
