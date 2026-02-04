#!/bin/bash
# Browser Challenge Agent - Run Script

cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Run the agent
cd agent
python main.py "$@"
