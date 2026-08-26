// Reads 155-byte YSFD packets as hex, one per line, and reports what
// urfd's own decoders make of them.
#include <cstdio>
#include <cstring>
#include "YSFFich.h"
#include "YSFPayload.h"
#include "YSFDefines.h"

int main() {
    char line[8192];
    while (fgets(line, sizeof(line), stdin)) {
        size_t n = strlen(line);
        while (n && (line[n-1] == '\n' || line[n-1] == '\r')) line[--n] = 0;
        if (n != 310) { printf("BADLEN %zu\n", n/2); continue; }
        unsigned char buf[155];
        for (int i = 0; i < 155; i++) {
            unsigned int v; sscanf(line + 2*i, "%2x", &v); buf[i] = (unsigned char)v;
        }
        if (memcmp(buf, "YSFD", 4)) { printf("BADTAG\n"); continue; }

        CYSFFICH fich;
        bool ok = fich.decode(buf + 40);
        if (!ok) { printf("FICH-REJECTED\n"); continue; }
        printf("fich=ok FI=%u DT=%u FN=%u FT=%u CS=%u SQL=%u SQ=%u",
               fich.getFI(), fich.getDT(), fich.getFN(), fich.getFT(),
               fich.getCS(), fich.getSQL()?1:0, fich.getSQ());

        unsigned char tmp[120];
        memcpy(tmp, buf + 35, 120);
        CYSFPayload p;
        if (fich.getFI() == YSF_FI_HEADER || fich.getFI() == YSF_FI_TERMINATOR) {
            printf(" header=%d", p.processHeaderData(tmp) ? 1 : 0);
        } else if (fich.getFI() == YSF_FI_COMMUNICATIONS) {
            unsigned char dt[11]; memset(dt, 0, sizeof(dt));
            bool d = p.readVDMode2Data(tmp, dt);
            printf(" dch=%d '%.10s'", d ? 1 : 0, dt);
        }
        printf(" src='%.10s'\n", buf + 14);
    }
    return 0;
}
