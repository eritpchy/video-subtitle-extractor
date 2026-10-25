"""Reconstruct visual OCR rows while retaining same-line text fragments."""
import math
import unicodedata


def join_visual_rows(boxes):
    entries = []
    for coordinate, text in boxes:
        if len(coordinate) != 4 or not all(math.isfinite(value) for value in coordinate):
            raise ValueError("Invalid OCR text rectangle")
        xmin, xmax, ymin, ymax = coordinate
        if xmax <= xmin or ymax <= ymin:
            raise ValueError("Empty OCR text rectangle")
        text = unicodedata.normalize('NFKC', text).strip()
        if text:
            entries.append((xmin, xmax, ymin, ymax, text))
    rows = []
    for entry in sorted(entries, key=lambda item: ((item[2] + item[3]) / 2, item[0])):
        _, _, ymin, ymax, _ = entry
        matching = []
        for index, row in enumerate(rows):
            # Compare to the row anchor rather than its growing union, so a
            # tall/overlapping box cannot chain two separate lines together.
            anchor = row[0]
            overlap = min(ymax, anchor[3]) - max(ymin, anchor[2])
            if overlap >= 0.5 * min(ymax - ymin, anchor[3] - anchor[2]):
                matching.append((abs((ymin + ymax) - (anchor[2] + anchor[3])), index))
        if matching:
            rows[min(matching)[1]].append(entry)
        else:
            rows.append([entry])
    rows.sort(key=lambda row: min(item[2] for item in row))
    return '\n'.join(' '.join(item[4] for item in sorted(row, key=lambda item: item[0]))
                     for row in rows)
