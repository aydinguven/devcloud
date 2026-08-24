import uvicorn
from app.config import settings

if __name__ == "__main__":
    print(f"Starting {settings.APP_NAME}...")
    print(f"Podman mode: {'MOCK (simulated)' if settings.USE_MOCK_PODMAN else 'AUTO-DETECT'}")
    print("Serving on http://127.0.0.1:8000")
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG,
    )
