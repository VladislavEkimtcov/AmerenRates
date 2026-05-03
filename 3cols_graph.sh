#!/bin/bash

# Gracefully handle termination to avoid hanging processes
trap "exit 0" SIGINT SIGTERM

# Run 3cols.py every 5 seconds forever
while true; do
    clear
    nice python3 3cols_combo.py -bar 7
    sleep 5
done
