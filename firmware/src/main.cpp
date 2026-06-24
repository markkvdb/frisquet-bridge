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
#define CONFIG_WHITE                                                           \
  (RH_RF69_PACKETCONFIG1_PACKETFORMAT_VARIABLE |                               \
   RH_RF69_PACKETCONFIG1_DCFREE_NONE | RH_RF69_PACKETCONFIG1_CRC_ON |           \
   RH_RF69_PACKETCONFIG1_CRCAUTOCLEAROFF |                                     \
   RH_RF69_PACKETCONFIG1_ADDRESSFILTERING_NONE)

RH_RF69 rf69(RFM69_CS, RFM69_INT);

enum class Mode : uint8_t { Idle, Listen, Sleep };

static Mode gMode = Mode::Idle;
static uint8_t gSeq = 0;
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

static bool handleTx(const char* hex, size_t hexLen, uint8_t seq) {
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

static bool handleNid(const char* hex, size_t hexLen, uint8_t seq) {
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
    protocol::emitErr(gSeq, "bad_crc");
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

  uint8_t seq = 0;

  if (strncmp(line, "NID ", 4) == 0) {
    handleNid(line + 4, len - 4, seq);
    return;
  }

  if (strncmp(line, "TX ", 3) == 0) {
    handleTx(line + 3, len - 3, seq);
    return;
  }

  if (strcmp(line, "LISTEN") == 0) {
    gMode = Mode::Listen;
    rf69.setPromiscuous(true);
    gLastHbMs = millis();
    protocol::emitOk(seq);
    return;
  }

  if (strcmp(line, "SLEEP") == 0) {
    gMode = Mode::Sleep;
    protocol::emitOk(seq);
    return;
  }

  if (strncmp(line, "PING ", 5) == 0) {
    seq = static_cast<uint8_t>(atoi(line + 5));
    protocol::emitPong(seq);
    return;
  }

  if (strcmp(line, "VERSION") == 0) {
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
      protocol::emitErr(0, "line_overflow");
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
