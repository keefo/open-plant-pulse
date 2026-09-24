idf_component_register(
    SRCS "app_main.c" "bthome_broadcaster.c" "clock_sync.c" "device_config_store.c" "device_identity.c" "force_report.c" "sht45_monitor.c" "web_ui.c" "web_ui_config.c"
    INCLUDE_DIRS "."
    REQUIRES bthome_payload clock_policy device_config_protocol power_source report_ack_protocol sensor_protocol sht45 bt esp_app_format esp_driver_tsens esp_driver_usb_serial_jtag esp_event esp_hw_support esp_http_server esp_netif esp_timer esp_wifi json nvs_flash
    EMBED_FILES "web/index.html" "web/app.css" "web/app.js"
)