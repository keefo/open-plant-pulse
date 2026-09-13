idf_component_register(
    SRCS "app_main.c" "web_ui.c" "web_ui_config.c"
    INCLUDE_DIRS "."
    REQUIRES bthome_payload sensor_protocol esp_app_format esp_driver_tsens esp_event esp_http_server esp_netif esp_timer esp_wifi json nvs_flash
    EMBED_FILES "web/index.html" "web/app.css" "web/app.js"
)