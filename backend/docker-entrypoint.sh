#!/bin/sh

set -eu

echo "Applying database migrations before starting the API..."
alembic upgrade head

exec "$@"
