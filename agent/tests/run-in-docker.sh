#!/bin/sh
# Builds the agent image and runs all agent tests in it (the browser tests need Playwright and Chromium).
# Call from the repository root. The container gets no network: the test pages are served on 127.0.0.1 inside it.
set -eu
# --load: with a docker-container buildx builder the image would otherwise stay in the build cache only
docker build -q --load -t unsubscriber-agent-test agent >/dev/null
docker run --rm --network none --user pwuser -v "$PWD/agent/tests:/app/tests:ro" unsubscriber-agent-test \
  python -m pytest -q -p no:cacheprovider /app/tests
