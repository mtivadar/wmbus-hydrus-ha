# sample_telegrams.txt

About 7.5 hours of rtl_wmbus output (T1 mode) from one Diehl Hydrus water meter. It covers an
evening with two showers and a long shower, so `replay.py` finds 23 steps = 230 L.

Anonymised: the meter id is replaced by `12345678`, both in the id column and in the A-field
bytes of the telegram. Dates are shifted to a fictitious day; the time of day is kept because
the episode classifier uses it. Only frames from this one meter are included.

Format (one frame per line, as printed by rtl_wmbus):

    mode;n;n;timestamp;rssi1;rssi2;meter_id;0x<telegram without CRCs>
