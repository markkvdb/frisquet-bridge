#pragma once

#include <Arduino.h>

namespace protocol {

static constexpr char kName[] = "frisquet-bridge-fw";
static constexpr char kVersion[] = "1.0.0";

uint8_t crc8(const uint8_t* data, size_t len);
bool crc8Matches(const char* line, size_t lineLen);

bool parseHex(const char* hex, size_t hexLen, uint8_t* out, size_t* outLen);
void appendHexByte(uint8_t value, char* out, size_t* pos, size_t maxLen);

void emitReady();
void emitRx(int16_t rssi, const uint8_t* frame, size_t frameLen);
void emitOk(uint8_t seq);
void emitErr(uint8_t seq, const char* reason);
void emitPong(uint8_t seq);
void emitInfo(const char* key, const char* value);
void emitHeartbeat();

}  // namespace protocol
