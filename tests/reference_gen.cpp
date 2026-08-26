// Generates reference vectors from urfd's own YSF encoders.
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include "YSFFich.h"
#include "YSFPayload.h"
#include "YSFDefines.h"

static unsigned int rnd_state = 12345;
static unsigned int rnd() { rnd_state = rnd_state * 1103515245u + 12345u; return (rnd_state >> 16) & 0x7FFFu; }

static void dump(const char* tag, const unsigned char* p, unsigned int n) {
    printf("%s ", tag);
    for (unsigned int i = 0; i < n; i++) printf("%02X", p[i]);
    printf("\n");
}

int main() {
    // FICH: sweep the fields a gateway would set
    for (int t = 0; t < 200; t++) {
        unsigned char fi = rnd() % 4, cs = rnd() % 4, cm = rnd() % 4;
        unsigned char bn = rnd() % 16, bt = rnd() % 16;
        unsigned char fn = rnd() % 8, ft = rnd() % 8;
        unsigned char dt = rnd() % 4, mr = rnd() % 8, sq = rnd() % 128;
        bool dev = rnd() % 2, sql = rnd() % 2, voip = rnd() % 2;

        CYSFFICH fich;
        fich.setFI(fi); fich.setCS(cs); fich.setCM(cm);
        fich.setBN(bn); fich.setBT(bt);
        fich.setFN(fn); fich.setFT(ft);
        fich.setDT(dt); fich.setMR(mr);
        fich.setDev(dev); fich.setSQL(sql); fich.setSQ(sq);
        fich.setVoIP(voip);

        unsigned char out[25];
        memset(out, 0, sizeof(out));
        fich.encode(out);
        printf("FICH %u %u %u %u %u %u %u %u %u %u %d %d %d ",
               fi, cs, cm, bn, bt, fn, ft, dt, mr, sq, dev?1:0, sql?1:0, voip?1:0);
        for (int i = 0; i < 25; i++) printf("%02X", out[i]);
        printf("\n");
    }

    // writeVDMode2Data: the DCH, ten characters at a time
    for (int t = 0; t < 100; t++) {
        unsigned char dt[10];
        for (int i = 0; i < 10; i++) dt[i] = 0x20 + (rnd() % 0x5F);
        unsigned char frame[120];
        memset(frame, 0, sizeof(frame));
        CYSFPayload p;
        p.writeVDMode2Data(frame, dt);
        printf("DCH ");
        for (int i = 0; i < 10; i++) printf("%02X", dt[i]);
        printf(" ");
        for (int i = 0; i < 120; i++) printf("%02X", frame[i]);
        printf("\n");
    }
    // writeHeader: the header and terminator payload
    for (int t = 0; t < 100; t++) {
        unsigned char csd1[20], csd2[20];
        for (int i = 0; i < 20; i++) csd1[i] = 0x20 + (rnd() % 0x5F);
        for (int i = 0; i < 20; i++) csd2[i] = 0x20 + (rnd() % 0x5F);
        unsigned char frame[120];
        memset(frame, 0, sizeof(frame));
        CYSFPayload p;
        p.writeHeader(frame, csd1, csd2);
        printf("HDR ");
        for (int i = 0; i < 20; i++) printf("%02X", csd1[i]);
        printf(" ");
        for (int i = 0; i < 20; i++) printf("%02X", csd2[i]);
        printf(" ");
        for (int i = 0; i < 120; i++) printf("%02X", frame[i]);
        printf("\n");
    }
    return 0;
}
