from fastapi import FastAPI


def create_app() -> FastAPI:
    return FastAPI(
        title="Bring Your Own Data Service",
        description="Upload CSV files as independent datasets and manage them via a REST API.",
        version="1.0.0",
    )


app = create_app()
