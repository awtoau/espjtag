/* espjtag RAM flasher stub (#27) — a resident mailbox loop the debugger feeds
 * over SBA, eliminating the ~5 ms halt/resume/register cost of every ROM call.
 *
 * Contract (everything via the mailbox pointer in a0 — NO globals, NO absolute
 * addresses, so one position-independent blob serves every RISC-V ESP chip;
 * per-chip ROM entry points are written into the mailbox by the host):
 *
 *   m[0]  cmd      host writes LAST (after args): 1=CRC_RANGE 2=ERASE_PROG 3=QUIT
 *   m[1]  status   stub writes: 1=done 2=error   (host clears to 0 before cmd)
 *   m[2]  in: crc init value / out: error code
 *   m[3]  flash byte address
 *   m[4]  sector count (CRC_RANGE)
 *   m[5]  staging buffer pointer
 *   m[6]  crc results array pointer
 *   m[7]  ROM esp_rom_spiflash_read(addr, buf, len)
 *   m[8]  ROM esp_rom_crc32_le(init, buf, len)
 *   m[9]  ROM esp_rom_spiflash_unlock()
 *   m[10] ROM esp_rom_spiflash_erase_sector(secno)
 *   m[11] ROM esp_rom_spiflash_write(addr, buf, len)
 *
 * QUIT makes the stub return — ra points at the debugger's ebreak trap word, so
 * the hart re-enters debug mode and the host restores the app untouched.
 *
 * Build: scripts/build_stub.sh -> espjtag/blobs/stub_rv32.bin (Apache-2.0, ours).
 */
typedef unsigned int u32;
typedef int (*rom3_t)(u32, u32, u32);
typedef int (*rom1_t)(u32);
typedef int (*rom0_t)(void);
typedef u32 (*crc_t)(u32, const void *, u32);

#define CMD_IDLE 0u
#define CMD_CRC_RANGE 1u
#define CMD_ERASE_PROG 2u
#define CMD_QUIT 3u
#define SECTOR 4096u

void stub(volatile u32 *m)
{
    for (;;) {
        u32 cmd = m[0];
        if (cmd == CMD_IDLE)
            continue;
        m[0] = CMD_IDLE;
        u32 err = 0;
        if (cmd == CMD_CRC_RANGE) {
            rom3_t rd = (rom3_t)m[7];
            crc_t crc = (crc_t)m[8];
            u32 addr = m[3], nsec = m[4], buf = m[5], init = m[2];
            volatile u32 *out = (volatile u32 *)m[6];
            for (u32 s = 0; s < nsec && !err; s++) {
                err = (u32)rd(addr + s * SECTOR, buf, SECTOR);
                if (!err)
                    out[s] = crc(init, (const void *)buf, SECTOR);
            }
        } else if (cmd == CMD_ERASE_PROG) {
            rom0_t unlock = (rom0_t)m[9];
            rom1_t erase = (rom1_t)m[10];
            rom3_t write = (rom3_t)m[11];
            u32 addr = m[3], buf = m[5];
            err = (u32)unlock();
            if (!err)
                err = (u32)erase(addr >> 12);
            if (!err)
                err = (u32)write(addr, buf, SECTOR);
        } else if (cmd == CMD_QUIT) {
            m[1] = 1;
            return;             /* ra -> ebreak trap word: back to debug mode */
        }
        m[2] = err;
        m[1] = err ? 2u : 1u;
    }
}
