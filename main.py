import uvicorn
from fastapi import FastAPI

from review_runner.api import create_app
from review_runner.service import ReviewService

service = ReviewService(auto_start=True)
app: FastAPI = create_app(service)


@app.get("/")
def read_root():
    return {"service": "ai-code-review-runner", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
