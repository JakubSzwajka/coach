"""Real private adapter fixture for the signed-in Next server boundary test."""

from __future__ import annotations

import json
import threading

from cryptography.fernet import Fernet

from coach.application import ClerkActor, CoachApplication, EnsureProfile
from coach.http_adapter import CoachHttpService, HttpAdapterConfig, create_server
from coach.postgres import DatabaseSettings
from tests.postgres.support import test_database


def main() -> None:
    database = test_database()
    settings = DatabaseSettings.from_url(database.application_url)
    application = CoachApplication(settings)
    application.execute(
        ClerkActor("https://identity.example.test", "verified-web-actor"),
        EnsureProfile("Verified Web Actor"),
    )
    application.execute(
        ClerkActor("https://identity.example.test", "hostile-body-actor"),
        EnsureProfile("Hostile Body Actor"),
    )
    key = Fernet.generate_key()
    config = HttpAdapterConfig(
        "https://identity.example.test",
        "synthetic-next-service-token",
        key,
        "127.0.0.1",
        0,
    )
    server = create_server(CoachHttpService(application, config))
    unavailable = CoachApplication(
        DatabaseSettings.from_url(
            "postgresql://coach:synthetic@127.0.0.1:1/unavailable"
        )
    )
    outage_server = create_server(
        CoachHttpService(
            unavailable,
            HttpAdapterConfig(
                config.clerk_issuer,
                config.service_token,
                key,
                "127.0.0.1",
                0,
            ),
        )
    )
    outage_thread = threading.Thread(
        target=outage_server.serve_forever, daemon=True
    )
    outage_thread.start()
    print(
        json.dumps(
            {
                "port": server.server_address[1],
                "outage_port": outage_server.server_address[1],
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        outage_server.shutdown()
        outage_server.server_close()
        outage_thread.join(timeout=5)


if __name__ == "__main__":
    main()
