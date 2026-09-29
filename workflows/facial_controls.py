"""Conservative facial micro-control recognition; not all eye/head bones."""
import re


def is_facial_micro_control(name):
    value = re.sub(r'[\s_.:\-]+', '', str(name)).casefold()
    return bool(
        re.fullmatch(r'eyebrowstrand\d*[lr]?', value)
        or re.fullmatch(r'eye\d+[lr]?', value)
        or re.fullmatch(r'(?:face)?(?:lt|rt|md|left|right)?(?:eyelid|eyebrow|cheek|lip|mouth|jaw|nose|chin).*', value)
        or (value.startswith('face') and any(part in value for part in
            ('cheek', 'eyelid', 'eyebrow', 'lip', 'mouth', 'jaw', 'nose', 'chin')))
        or any(part in value for part in ('眉毛', 'まゆ', '頬', '頰', '脸颊', '眼睑', 'まぶた', '唇'))
    )
