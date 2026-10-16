// USB link: mac/scale.html in Chrome reads the serial port with Web Serial. No radio is used.
// Each new reading goes out as one line, "@" followed by JSON. Messages are printed by remote_log,
// and commands arrive as typed lines, both through the normal serial path in main.c.
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "remote.h"

static void status_task(void *arg)
{
    uint32_t sent = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(100));
        scale_reading_t r;
        uint32_t seq = remote_get_reading(&r);
        if (seq == sent) continue;
        char buf[200];
        remote_format_status(buf, sizeof buf);
        printf("@%s\n", buf);
        sent = seq;
    }
}

void remote_start(void)
{
    xTaskCreate(status_task, "usb_status", 3072, NULL, 5, NULL);
    remote_log("USB link: open mac/scale.html in Chrome and press USB");
}
