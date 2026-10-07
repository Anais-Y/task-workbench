#!/bin/sh
cd "$(dirname "$0")" || exit 1
./task-workbench start || exit 1
open 'http://127.0.0.1:8766/'
