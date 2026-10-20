cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# Firmware version, reported by the API. Raise it with each release.
set(PROJECT_VER "2.2.0")
project(hx711_scale)
