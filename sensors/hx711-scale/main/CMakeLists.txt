set(srcs "main.c" "hx711.c" "remote.c" "filter.c")
set(embed "")
if(CONFIG_SCALE_LINK_BLE)
    list(APPEND srcs "ble.c")
elseif(CONFIG_SCALE_LINK_USB)
    list(APPEND srcs "usb.c")
else()
    list(APPEND srcs "web.c")
    list(APPEND embed "index.html")
endif()

# Requirements are resolved before the config is read, so they cover both links.
idf_component_register(SRCS ${srcs}
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES driver esp_adc esp_driver_uart esp_driver_usb_serial_jtag esp_timer nvs_flash app_update
                                     bt esp_wifi esp_netif esp_event esp_http_server
                       EMBED_FILES ${embed})
