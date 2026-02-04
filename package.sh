#!/bin/bash
# Browser Challenge Agent - Package Script

cd "$(dirname "$0")"

# Create zip package
zip -r browser-challenge-agent.zip \
    agent/*.py \
    requirements.txt \
    README.md \
    .env.example \
    run.sh \
    -x "*.pyc" -x "__pycache__/*" -x ".env" -x "agent/test_*.py"

echo "Created: browser-challenge-agent.zip"
ls -lh browser-challenge-agent.zip
