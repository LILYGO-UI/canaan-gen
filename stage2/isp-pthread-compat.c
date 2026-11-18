// SPDX-License-Identifier: MIT
// The vendor ISP requires explicit RR threads. With CONFIG_RT_GROUP_SCHED,
// Linux 6.6 rejects them inside a cgroup-v2 CPU domain even for root.
// This preload is scoped to isp_media_server and preserves all other failures.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdio.h>

static int (*next_create)(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;
static atomic_flag warned = ATOMIC_FLAG_INIT;

static void resolve_create(void) {
    *(void **)(&next_create) = dlsym(RTLD_NEXT, "pthread_create");
}

int pthread_create(pthread_t *thread, const pthread_attr_t *attributes,
                   void *(*entry)(void *), void *argument) {
    pthread_once(&resolve_once, resolve_create);
    if (!next_create) return ENOSYS;
    int result = next_create(thread, attributes, entry, argument);
    if (result != EPERM || !attributes) return result;
    int policy = SCHED_OTHER;
    if (pthread_attr_getschedpolicy(attributes, &policy) != 0 ||
        (policy != SCHED_RR && policy != SCHED_FIFO)) return result;

    pthread_attr_t fallback;
    if (pthread_attr_init(&fallback) != 0) return result;
    int detach = PTHREAD_CREATE_JOINABLE;
    size_t stack_size = 0, guard_size = 0;
    void *stack = NULL;
    int error = pthread_attr_getdetachstate(attributes, &detach);
    if (!error) error = pthread_attr_setdetachstate(&fallback, detach);
    if (!error) error = pthread_attr_getguardsize(attributes, &guard_size);
    if (!error) error = pthread_attr_setguardsize(&fallback, guard_size);
    if (!error) error = pthread_attr_getstack(attributes, &stack, &stack_size);
    if (!error && stack) error = pthread_attr_setstack(&fallback, stack, stack_size);
    else if (!error && stack_size) error = pthread_attr_setstacksize(&fallback, stack_size);
    struct sched_param normal = {.sched_priority = 0};
    if (!error) error = pthread_attr_setschedpolicy(&fallback, SCHED_OTHER);
    if (!error) error = pthread_attr_setschedparam(&fallback, &normal);
    if (!error) error = pthread_attr_setinheritsched(&fallback, PTHREAD_EXPLICIT_SCHED);
    if (!error) {
        result = next_create(thread, &fallback, entry, argument);
        if (!result && !atomic_flag_test_and_set(&warned))
            fputs("K230 ISP: realtime thread denied; using SCHED_OTHER.\n", stderr);
    }
    pthread_attr_destroy(&fallback);
    return result;
}
