"""Seed this local demo through the pinned Open WebUI's native model APIs."""

import asyncio
import base64
import json
import os
from pathlib import Path


async def bootstrap():
    from open_webui.config import WEBUI_AUTH  # Also runs the image's database migrations.
    from open_webui.models.auths import Auths
    from open_webui.models.functions import FunctionForm, FunctionMeta, Functions
    from open_webui.models.models import ModelForm, ModelMeta, ModelParams, Models
    from open_webui.models.users import Users
    from open_webui.utils.auth import get_password_hash

    if WEBUI_AUTH:
        return

    user = await Users.get_user_by_email("admin@localhost")
    if user is None:
        if await Users.has_users():
            return
        # Match the account Open WebUI creates on first sign-in with WEBUI_AUTH=False.
        user = await Auths.insert_new_auth(
            "admin@localhost", await get_password_hash("admin"), "User", role="admin"
        )
        if user is None:
            raise RuntimeError("Open WebUI could not create its local demo user")

    assets = Path(__file__).parent
    if await Functions.get_function_by_id("picton_citations") is None:
        created = await Functions.insert_new_function(
            user.id,
            "filter",
            FunctionForm(
                id="picton_citations",
                name="Picton SEC citations",
                content=(assets / "openwebui_citations.py").read_text(),
                meta=FunctionMeta(description="Native citations for SEC source excerpts."),
            ),
        )
        if created is None:
            raise RuntimeError("Open WebUI could not create the Picton citation filter")
        activated = await Functions.update_function_by_id("picton_citations", {"is_active": True})
        if activated is None:
            raise RuntimeError("Open WebUI could not activate the Picton citation filter")

    if await Models.get_model_by_id("openai/gpt-6-luna") is None:
        metadata = json.loads(os.environ.get("DEFAULT_MODEL_METADATA", "{}"))
        metadata["filterIds"] = ["picton_citations"]
        metadata["profile_image_url"] = (
            "data:image/png;base64,"
            + base64.b64encode((assets / "picton-bear.png").read_bytes()).decode()
        )
        created = await Models.insert_new_model(
            ModelForm(
                id="openai/gpt-6-luna",
                name="Murph",
                meta=ModelMeta(**metadata),
                params=ModelParams(),
            ),
            user.id,
        )
        if created is None:
            raise RuntimeError("Open WebUI could not create the Murph model")


if __name__ == "__main__":
    asyncio.run(bootstrap())
