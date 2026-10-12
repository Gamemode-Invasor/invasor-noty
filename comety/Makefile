CC      ?= gcc
CFLAGS  ?= -O2 -Wall -Wextra
CPPFLAGS = -Ideps/sqlite -DSQLITE_THREADSAFE=0 -DSQLITE_OMIT_LOAD_EXTENSION
LDFLAGS  = -static -s
LDLIBS   = -lm

SQLITE_DIR   = deps/sqlite
SQLITE_YEAR  = 2026
SQLITE_VER   = 3530400
SQLITE_ZIP   = sqlite-amalgamation-$(SQLITE_VER).zip
SQLITE_URL   = https://sqlite.org/$(SQLITE_YEAR)/$(SQLITE_ZIP)

TARGET = comety
OBJS   = comety.o $(SQLITE_DIR)/sqlite3.o

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $(OBJS) -o $@ $(LDLIBS)
	$(CC) $(CFLAGS) $(OBJS) -o $@.no-static $(LDLIBS)

comety.o: comety.c $(SQLITE_DIR)/sqlite3.h
	$(CC) $(CFLAGS) $(CPPFLAGS) -c comety.c -o $@

$(SQLITE_DIR)/sqlite3.o: $(SQLITE_DIR)/sqlite3.c $(SQLITE_DIR)/sqlite3.h
	$(CC) -O2 -w $(CPPFLAGS) -c $(SQLITE_DIR)/sqlite3.c -o $@

$(SQLITE_DIR)/sqlite3.c $(SQLITE_DIR)/sqlite3.h:
	@mkdir -p $(SQLITE_DIR) deps/download
	curl -sL -o deps/download/$(SQLITE_ZIP) $(SQLITE_URL)
	unzip -o -j deps/download/$(SQLITE_ZIP) '*/sqlite3.c' '*/sqlite3.h' '*/sqlite3ext.h' -d $(SQLITE_DIR)

clean:
	rm -f $(TARGET) comety.o $(SQLITE_DIR)/sqlite3.o

.PHONY: all clean
