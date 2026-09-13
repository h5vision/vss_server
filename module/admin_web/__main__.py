"""Run the independent Admin Web on its fixed service port."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "admin_web.app:create_app_from_environment",
        factory=True,
        host=os.getenv("ADMIN_WEB_HOST", "0.0.0.0"),
        port=4180,
    )

