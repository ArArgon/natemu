// Native networking admission only: no packets, guest RAM, privileges or persistent interface.
#include <vmnet/vmnet.h>
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static void wait_for(dispatch_semaphore_t done, const char *phase) {
    if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC))) {
        fprintf(stderr, "%s timed out; process exit retires callback storage\n", phase);
        _exit(124);
    }
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    dispatch_queue_t queue = dispatch_queue_create("natemu.vmnet.admission", DISPATCH_QUEUE_SERIAL);
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    xpc_object_t desc = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_uint64(desc, vmnet_operation_mode_key, VMNET_SHARED_MODE);
    xpc_dictionary_set_uint64(desc, vmnet_mtu_key, 1500);
    __block vmnet_return_t start_status = VMNET_FAILURE;
    __block uint64_t mtu = 0, maximum = 0;
    interface_ref interface = vmnet_start_interface(desc, queue, ^(vmnet_return_t status, xpc_object_t params) {
        start_status = status;
        if (status == VMNET_SUCCESS && params) {
            mtu = xpc_dictionary_get_uint64(params, vmnet_mtu_key);
            maximum = xpc_dictionary_get_uint64(params, vmnet_max_packet_size_key);
        }
        dispatch_semaphore_signal(done);
    });
    xpc_release(desc);
    if (!interface) {
        printf("{\"uid\":%u,\"handle\":false}\n", getuid());
        return 1;
    }
    wait_for(done, "start");
    dispatch_sync(queue, ^{});
    printf("{\"uid\":%u,\"handle\":true,\"start\":%u,\"mtu\":%llu,\"maximum\":%llu}\n",
           getuid(), start_status, (unsigned long long)mtu, (unsigned long long)maximum);
    if (start_status != VMNET_SUCCESS) return 1;
    __block vmnet_return_t stopped = VMNET_FAILURE;
    vmnet_return_t scheduled = vmnet_stop_interface(interface, queue, ^(vmnet_return_t status) {
        stopped = status;
        dispatch_semaphore_signal(done);
    });
    if (scheduled != VMNET_SUCCESS) {
        printf("{\"stop_scheduled\":%u}\n", scheduled);
        return 1;
    }
    wait_for(done, "stop");
    dispatch_sync(queue, ^{});
    printf("{\"stop\":%u}\n", stopped);
    dispatch_release(done);
    dispatch_release(queue);
    return stopped == VMNET_SUCCESS && mtu == 1500 && maximum > 0 && maximum <= 2048 ? 0 : 1;
}
