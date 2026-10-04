CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra -Werror -std=c11

all: capture-worker

capture-worker: capture-worker.c
	$(CC) $(CFLAGS) -pthread -o $@ $<

test: capture-worker
	./capture-worker --self-test
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

clean:
	rm -f capture-worker

.PHONY: all test clean
