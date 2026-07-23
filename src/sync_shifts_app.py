import modal

app = modal.App("foodcoop.calendar")

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync()
    .run_commands("playwright install --with-deps chromium")
)


@app.function(
    image=image,
    schedule=modal.Cron("*/20 * * * *", timezone="America/New_York"),
    secrets=[
        modal.Secret.from_name("foodcoop-credentials"),
        modal.Secret.from_name("google-service-account"),
    ],
)
async def sync_shifts():
    from . import main

    await main.main()
