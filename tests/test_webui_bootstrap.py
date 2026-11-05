"""Opt-in bootstrap check against the pinned image, without existing volumes or a network."""

import os
import subprocess
import unittest

from picton_graph.config import ROOT


@unittest.skipUnless(os.getenv("WEBUI_BOOTSTRAP_IMAGE"), "Set WEBUI_BOOTSTRAP_IMAGE to test Docker")
class WebUIBootstrapTests(unittest.TestCase):
    def test_first_start_and_preserve_existing_settings(self):
        command = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "-e",
            "WEBUI_AUTH=False",
            "-e",
            "WEBUI_SECRET_KEY=isolated-bootstrap-test",
            "-e",
            "DATA_DIR=/tmp/picton-bootstrap-test",
            "-e",
            "HF_HUB_OFFLINE=1",
            "-e",
            'DEFAULT_MODEL_METADATA={"toolIds":["server:mcp:picton"]}',
        ]
        for source, destination in (
            ("deploy/bootstrap_webui.py", "picton_bootstrap.py"),
            ("deploy/picton-bear.png", "picton-bear.png"),
            ("picton_graph/integrations/openwebui_citations.py", "openwebui_citations.py"),
        ):
            command.extend(["-v", f"{ROOT / source}:/app/backend/{destination}:ro"])
        command.extend([os.environ["WEBUI_BOOTSTRAP_IMAGE"], "-"])
        result = subprocess.run(
            command,
            input="""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
Path('/tmp/picton-bootstrap-test').mkdir()
entrypoint = [sys.executable, '/app/backend/picton_bootstrap.py']
subprocess.run(entrypoint, env={**os.environ, 'WEBUI_AUTH': 'True'}, cwd='/', check=True)
from open_webui import config
from open_webui.models.functions import Functions
from open_webui.models.models import Model, Models
from open_webui.models.users import Users
from open_webui.internal.db import get_async_db_context

async def check():
    assert not await Users.has_users(), 'Do not create a demo admin when auth is enabled'
    subprocess.run(entrypoint, cwd='/', check=True)
    user = await Users.get_user_by_email('admin@localhost')
    assert user and user.role == 'admin'
    function = await Functions.get_function_by_id('picton_citations')
    assert function.is_active and 'class Filter' in function.content
    model = await Models.get_model_by_id('openai/gpt-6-luna')
    assert model.name == 'Murph'
    assert model.meta.profile_image_url.startswith('data:image/png;base64,')
    assert model.meta.model_dump()['filterIds'] == ['picton_citations']
    assert model.meta.model_dump()['toolIds'] == ['server:mcp:picton']
    await Functions.update_function_by_id(
        'picton_citations', {'content': 'custom filter', 'is_active': False}
    )
    async with get_async_db_context() as db:
        row = await db.get(Model, 'openai/gpt-6-luna')
        row.name = 'Custom name'
        await db.commit()
    subprocess.run(entrypoint, cwd='/', check=True)
    assert (await Models.get_model_by_id('openai/gpt-6-luna')).name == 'Custom name'
    function = await Functions.get_function_by_id('picton_citations')
    assert function.content == 'custom filter' and not function.is_active
asyncio.run(check())
""",
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
