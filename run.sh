#!/bin/bash

docker compose up -d --build || { echo "Docker compose up failed!"; exit 1; }