#!/bin/bash

# Run 3cols.py every 5 seconds forever
while true; do
    python3 3cols_combo.py --max-bar-width 7
    sleep 5
done
