# Virtual Desktop codec enum (VirtualDesktop.Net.VideoCodec), reflected from VirtualDesktop.Streamer.exe 1.34.18

| codec | PreferredCodec value |
|---|---|
| Automatic | 0 |
| H.264 | 1 |
| HEVC | 2 |
| VP8 | 3 |
| VP9 | 4 |
| H.264+ (H264Plus) | 5 |
| HEVC 10-bit (HEVC10bit) | 6 |
| AV1 | 10 |
| AV1 10-bit (AV110bit) | 11 |

Matrix codecs: vd-h264p-* -> 5 (H264Plus); vd-hevc-* -> 6 (HEVC10bit); vd-av1-* -> 11 (AV110bit).
Set via `StreamerSettings.json` `PreferredCodec` (+ `CodecName` display string), then restart the streamer.
