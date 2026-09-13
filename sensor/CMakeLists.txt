cmake_minimum_required(VERSION 3.16)

file(READ "${CMAKE_CURRENT_LIST_DIR}/version.txt" OPP_FIRMWARE_VERSION)
string(STRIP "${OPP_FIRMWARE_VERSION}" OPP_FIRMWARE_VERSION)
if(NOT OPP_FIRMWARE_VERSION MATCHES "^[0-9]+\\.[0-9]+\\.[0-9]+(-[0-9A-Za-z.-]+)?(\\+[0-9A-Za-z.-]+)?$")
	message(FATAL_ERROR "sensor/version.txt must contain a Semantic Version, such as 1.2.3")
endif()
string(LENGTH "${OPP_FIRMWARE_VERSION}" OPP_FIRMWARE_VERSION_LENGTH)
if(OPP_FIRMWARE_VERSION_LENGTH GREATER 31)
	message(FATAL_ERROR "sensor/version.txt must not exceed ESP-IDF's 31-character application version limit")
endif()

set(EXTRA_COMPONENT_DIRS
	"${CMAKE_CURRENT_LIST_DIR}/firmware/main"
	"${CMAKE_CURRENT_LIST_DIR}/firmware/components"
)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(open_plant_pulse)