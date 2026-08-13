#include "protocol.h"

#include <SPI.h>
#include <RH_RF69.h>

// Feather M0 w/ RFM69 on-board
#define RFM69_CS 8
#define RFM69_INT 3
#define RFM69_RST 4
#define LED 13

#define RF69_FREQ 868.96

// From RH_RF69.cpp — Frisquet FSK modem profile
#define CONFIG_FSK                                                             \
  (RH_RF69_DATAMODUL_DATAMODE_PACKET | RH_RF69_DATAMODUL_MODULATIONTYPE_FSK |  \
   RH_RF69_DATAMODUL_MODULATIONSHAPING_FSK_NONE)
// Leave CrcAutoClearOff unset so the RFM69 discards RF packets whose CRC
// fails; forwarding them caused one-bit-shifted 0x79e0 temperatures.
#define CONFIG_WHITE                                                           \
  (RH_RF69_PACKETCONFIG1_PACKETFORMAT_VARIABLE |                               \
   RH_RF69_PACKETCONFIG1_DCFREE_NONE | RH_RF69_PACKETCONFIG1_CRC_ON |           \
   RH_RF69_PACKETCONFIG1_ADDRESSFILTERING_NONE)

RH_RF69 rf69(RFM69_CS, RFM69_INT);

enum class Mode : uint8_t { Idle, Listen, Sleep };

static Mode gMode = Mode::Idle;

static uint32_t gLastHbMs = 0;
static constexpr uint32_t kHeartbeatMs = 30000;

// Line input buffer (no String heap fragmentation)
static char gLine[384];
static size_t gLineLen = 0;

static bool radioInit() {
  pinMode(LED, OUTPUT);
  pinMode(RFM69_RST, OUTPUT);
  digitalWrite(RFM69_RST, LOW);
  delay(10);
  digitalWrite(RFM69_RST, HIGH);
  delay(10);
  digitalWrite(RFM69_RST, LOW);
  delay(10);

  if (!rf69.init()) {
    return false;
  }
  if (!rf69.setFrequency(RF69_FREQ)) {
    return false;
  }

  rf69.setTxPower(20, true);

  const RH_RF69::ModemConfig config{
      CONFIG_FSK, 0x05, 0x00, 0x03, 0x34,
      0b01010001, 0b01010001, CONFIG_WHITE};
  rf69.setModemRegisters(&config);
  rf69.setPreambleLength(4);

  uint8_t syncwords[] = {0xff, 0xff, 0xff, 0xff};
  rf69.setSyncWords(syncwords, sizeof(syncwords));
  rf69.setPromiscuous(true);

  return true;
}

static void ledRxPulse() {
  digitalWrite(LED, HIGH);
  delay(2);
  digitalWrite(LED, LOW);
}

// Reconstruct Rust-compatible frame from RadioHead RX
static void emitReceivedPacket() {
  uint8_t buf[RH_RF69_MAX_MESSAGE_LEN];
  uint8_t len = sizeof(buf);
  if (!rf69.recv(buf, &len) || len == 0) {
    return;
  }

  // RH payload = [control, msg_type, ...data]
  // Rust length byte = 6 + (len - 2) = len + 4
  uint8_t frame[RH_RF69_MAX_MESSAGE_LEN + 7];
  size_t frameLen = 0;
  frame[frameLen++] = static_cast<uint8_t>(len + 4);
  frame[frameLen++] = rf69.headerTo();
  frame[frameLen++] = rf69.headerFrom();
  frame[frameLen++] = rf69.headerId();
  frame[frameLen++] = rf69.headerFlags();
  for (uint8_t i = 0; i < len; i++) {
    frame[frameLen++] = buf[i];
  }

  protocol::emitRx(rf69.lastRssi(), frame, frameLen);
  ledRxPulse();
}

static bool handleTx(const char* hex, size_t hexLen, uint32_t seq) {
  uint8_t buf[255];
  size_t bufLen = sizeof(buf);
  if (!protocol::parseHex(hex, hexLen, buf, &bufLen) || bufLen < 6) {
    protocol::emitErr(seq, "bad_hex");
    return false;
  }

  uint8_t rhLen = buf[0];
  if (rhLen < 4 || static_cast<size_t>(rhLen - 4) > bufLen - 5) {
    protocol::emitErr(seq, "bad_frame");
    return false;
  }

  rf69.setHeaderTo(buf[1]);
  rf69.setHeaderFrom(buf[2]);
  rf69.setHeaderId(buf[3]);
  rf69.setHeaderFlags(buf[4], 0xff);

  if (!rf69.send(buf + 5, rhLen - 4)) {
    protocol::emitErr(seq, "tx_fail");
    return false;
  }

  protocol::emitOk(seq);
  return true;
}

static bool handleNid(const char* hex, size_t hexLen, uint32_t seq) {
  uint8_t sync[4];
  size_t syncLen = sizeof(sync);
  if (!protocol::parseHex(hex, hexLen, sync, &syncLen) || syncLen != 4) {
    protocol::emitErr(seq, "bad_hex");
    return false;
  }
  rf69.setSyncWords(sync, 4);
  protocol::emitOk(seq);
  return true;
}

static bool parseSequenceToken(const char* token, uint32_t* seq, const char** arguments) {
  if (token == nullptr || token[0] != '@' || token[1] < '0' || token[1] > '9') {
    return false;
  }
  uint32_t parsed = 0;
  const char* cursor = token + 1;
  while (*cursor >= '0' && *cursor <= '9') {
    uint8_t digit = static_cast<uint8_t>(*cursor - '0');
    if (parsed > (UINT32_MAX - digit) / 10U) {
      return false;
    }
    parsed = parsed * 10U + digit;
    cursor++;
  }
  if (*cursor != ' ' && *cursor != '\0') {
    return false;
  }
  *seq = parsed;
  *arguments = *cursor == ' ' ? cursor + 1 : cursor;
  return true;
}

static bool extractSequenceBeforeCrc(const char* line, size_t len, uint32_t* seq) {
  size_t crcSpace = len;
  while (crcSpace > 0 && line[crcSpace - 1] != ' ') {
    crcSpace--;
  }
  if (crcSpace == 0) {
    return false;
  }
  const char* firstSpace = strchr(line, ' ');
  if (firstSpace == nullptr || firstSpace + 1 >= line + crcSpace || firstSpace[1] != '@') {
    return false;
  }
  char token[12];
  size_t tokenLen = 0;
  const char* cursor = firstSpace + 1;
  while (cursor + tokenLen < line + crcSpace - 1 && cursor[tokenLen] != ' ') {
    if (tokenLen + 1 >= sizeof(token)) {
      return false;
    }
    token[tokenLen] = cursor[tokenLen];
    tokenLen++;
  }
  token[tokenLen] = '\0';
  const char* ignored = nullptr;
  return parseSequenceToken(token, seq, &ignored) && ignored[0] == '\0';
}

static void dispatchLine(char* line, size_t len) {
  if (len == 0) {
    return;
  }

  // Strip trailing whitespace
  while (len > 0 && (line[len - 1] == ' ' || line[len - 1] == '\t')) {
    len--;
  }
  line[len] = '\0';

  if (len == 0) {
    return;
  }

  if (!protocol::crc8Matches(line, len)) {
    uint32_t seq = 0;
    if (extractSequenceBeforeCrc(line, len, &seq)) {
      protocol::emitErr(seq, "bad_crc");
    } else {
      protocol::emitUncorrelatedErr("bad_crc");
    }
    return;
  }

  // Remove CRC token for parsing
  size_t crcSpace = len;
  while (crcSpace > 0 && line[crcSpace - 1] != ' ') {
    crcSpace--;
  }
  if (crcSpace > 0) {
    len = crcSpace - 1;
    line[len] = '\0';
  }

  uint32_t seq = 0;
  char* firstSpace = strchr(line, ' ');
  const char* arguments = nullptr;
  bool sequenced = firstSpace != nullptr && firstSpace[1] == '@';
  if (sequenced && !parseSequenceToken(firstSpace + 1, &seq, &arguments)) {
    protocol::emitUncorrelatedErr("bad_seq");
    return;
  }

  if (strncmp(line, "NID ", 4) == 0) {
    const char* hex = arguments != nullptr ? arguments : firstSpace + 1;
    handleNid(hex, strlen(hex), seq);
    return;
  }

  if (strncmp(line, "TX ", 3) == 0) {
    const char* hex = arguments != nullptr ? arguments : firstSpace + 1;
    handleTx(hex, strlen(hex), seq);
    return;
  }

  if (strcmp(line, "LISTEN") == 0 || (sequenced && strcmp(arguments, "") == 0 && strncmp(line, "LISTEN ", 7) == 0)) {
    gMode = Mode::Listen;
    rf69.setPromiscuous(true);
    gLastHbMs = millis();
    protocol::emitOk(seq);
    return;
  }

  if (strcmp(line, "SLEEP") == 0 || (sequenced && strcmp(arguments, "") == 0 && strncmp(line, "SLEEP ", 6) == 0)) {
    gMode = Mode::Sleep;
    protocol::emitOk(seq);
    return;
  }

  if ((sequenced && strncmp(line, "PING ", 5) == 0 && strcmp(arguments, "") == 0) ||
      (!sequenced && strncmp(line, "PING ", 5) == 0)) {
    if (!sequenced) {
      char* end = nullptr;
      unsigned long parsed = strtoul(line + 5, &end, 10);
      if (end == line + 5 || *end != '\0' || static_cast<unsigned long>(static_cast<uint32_t>(parsed)) != parsed) {
        protocol::emitUncorrelatedErr("bad_seq");
        return;
      }
      seq = static_cast<uint32_t>(parsed);
    }
    protocol::emitPong(seq);
    return;
  }

  if (strcmp(line, "VERSION") == 0 ||
      (sequenced && strcmp(arguments, "") == 0 && strncmp(line, "VERSION ", 8) == 0)) {
    protocol::emitReady();
    protocol::emitInfo("version", protocol::kVersion);
    protocol::emitOk(seq);
    return;
  }

  protocol::emitErr(seq, "unknown");
}

static void pollSerial() {
  while (Serial.available() > 0) {
    char c = static_cast<char>(Serial.read());
    if (c == '\r') {
      continue;
    }
    if (c == '\n') {
      dispatchLine(gLine, gLineLen);
      gLineLen = 0;
      continue;
    }
    if (gLineLen + 1 < sizeof(gLine)) {
      gLine[gLineLen++] = c;
    } else {
      // Overflow — discard line
      gLineLen = 0;
      protocol::emitUncorrelatedErr("line_overflow");
    }
  }
}

static void pollRadio() {
  if (gMode != Mode::Listen) {
    return;
  }

  if (rf69.available()) {
    emitReceivedPacket();
  }

  uint32_t now = millis();
  if (now - gLastHbMs >= kHeartbeatMs) {
    gLastHbMs = now;
    protocol::emitHeartbeat();
  }
}

void setup() {
  Serial.begin(115200);
  while (!Serial && millis() < 3000) {
    delay(10);
  }

  if (!radioInit()) {
    Serial.println(F("ERR 0 radio_init_failed"));
    pinMode(LED, OUTPUT);
    while (true) {
      digitalWrite(LED, !digitalRead(LED));
      delay(200);
    }
  }

  protocol::emitReady();
  gMode = Mode::Listen;
  gLastHbMs = millis();
}

void loop() {
  pollSerial();
  pollRadio();
}
