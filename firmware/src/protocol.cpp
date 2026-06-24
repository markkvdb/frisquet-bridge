#include "protocol.h"

#include <stdio.h>

namespace protocol {

uint8_t crc8(const uint8_t* data, size_t len) {
  uint8_t c = 0;
  for (size_t i = 0; i < len; i++) {
    c ^= data[i];
  }
  return c;
}

bool crc8Matches(const char* line, size_t lineLen) {
  if (lineLen < 4) {
    return false;
  }
  // Last token: two hex digits after final space
  size_t space = lineLen;
  while (space > 0 && line[space - 1] != ' ') {
    space--;
  }
  if (space == 0 || lineLen - space != 2) {
    return false;
  }
  char hi = line[space];
  char lo = line[space + 1];
  auto nibble = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  int h = nibble(hi);
  int l = nibble(lo);
  if (h < 0 || l < 0) {
    return false;
  }
  uint8_t expected = static_cast<uint8_t>((h << 4) | l);
  uint8_t actual = crc8(reinterpret_cast<const uint8_t*>(line), space - 1);
  return expected == actual;
}

bool parseHex(const char* hex, size_t hexLen, uint8_t* out, size_t* outLen) {
  if (hexLen % 2 != 0) {
    return false;
  }
  size_t max = *outLen;
  size_t n = hexLen / 2;
  if (n > max) {
    return false;
  }
  auto nibble = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
  };
  for (size_t i = 0; i < n; i++) {
    int hi = nibble(hex[i * 2]);
    int lo = nibble(hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) {
      return false;
    }
    out[i] = static_cast<uint8_t>((hi << 4) | lo);
  }
  *outLen = n;
  return true;
}

void appendHexByte(uint8_t value, char* out, size_t* pos, size_t maxLen) {
  if (*pos + 2 >= maxLen) {
    return;
  }
  static const char* kHex = "0123456789abcdef";
  out[(*pos)++] = kHex[(value >> 4) & 0x0f];
  out[(*pos)++] = kHex[value & 0x0f];
}

void emitReady() {
  Serial.print(F("READY "));
  Serial.print(kName);
  Serial.print(F(" "));
  Serial.println(kVersion);
}

void emitRx(int16_t rssi, const uint8_t* frame, size_t frameLen) {
  char line[512];
  size_t pos = 0;

  // "RX <rssi> "
  int n = snprintf(line, sizeof(line), "RX %d ", static_cast<int>(rssi));
  if (n < 0 || static_cast<size_t>(n) >= sizeof(line)) {
    return;
  }
  pos = static_cast<size_t>(n);

  for (size_t i = 0; i < frameLen; i++) {
    appendHexByte(frame[i], line, &pos, sizeof(line) - 4);
  }
  line[pos++] = ' ';
  uint8_t c = crc8(reinterpret_cast<const uint8_t*>(line), pos - 1);
  appendHexByte(c, line, &pos, sizeof(line));
  line[pos] = '\0';
  Serial.println(line);
}

void emitOk(uint8_t seq) {
  Serial.print(F("OK "));
  Serial.println(seq);
}

void emitErr(uint8_t seq, const char* reason) {
  Serial.print(F("ERR "));
  Serial.print(seq);
  Serial.print(F(" "));
  Serial.println(reason);
}

void emitPong(uint8_t seq) {
  Serial.print(F("PONG "));
  Serial.println(seq);
}

void emitInfo(const char* key, const char* value) {
  Serial.print(F("INFO "));
  Serial.print(key);
  Serial.print(F(" "));
  Serial.println(value);
}

void emitHeartbeat() {
  Serial.println(F("HB"));
}

}  // namespace protocol
