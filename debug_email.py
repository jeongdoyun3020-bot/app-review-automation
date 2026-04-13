import os
s = os.environ.get('SENDER_EMAIL', '')
domain = s.split('@')[-1] if '@' in s else 'NO_AT_SIGN'
print('SENDER_EMAIL domain: @' + domain)
print('SENDER_EMAIL length:', len(s))
