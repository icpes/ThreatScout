"""
pytest conftest — runs before any test collection.

Sets dummy Azure env vars so module-level client singletons can be constructed
without real credentials (AzureOpenAI stores the key; it only fails on actual
HTTP calls, which are always mocked in tests).

Also pre-imports each agent submodule so that `agents.watcher` etc. are real
attributes on the `agents` package — required for `patch("agents.watcher.client")`.
"""

import os

# Dummy credentials — only used for module construction, never for real calls
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-openai-key-00000000")
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://test-openai.openai.azure.com/")
os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")
os.environ.setdefault("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
os.environ.setdefault("AZURE_CONTENT_SAFETY_KEY", "test-content-safety-key-00000")
os.environ.setdefault("AZURE_CONTENT_SAFETY_ENDPOINT", "https://test-cs.cognitiveservices.azure.com/")

# Pre-import submodules so `patch("agents.watcher.client")` can resolve the path
import agents.watcher      # noqa: E402, F401
import agents.classifier   # noqa: E402, F401
import agents.analyst      # noqa: E402, F401
import agents.responder    # noqa: E402, F401
