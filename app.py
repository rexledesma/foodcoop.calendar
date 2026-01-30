import modal

app = modal.App("foodcoop-shift-calendar")

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync()
    .run_commands("playwright install --with-deps chromium")
    .add_local_python_source("main")
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
    import main

    await main.main()
