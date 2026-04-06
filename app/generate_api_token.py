import random
import string

def generate_api_token():
    api_token_lenth = 32
    api_token = ""
    alphanumeric_list = list(string.ascii_letters + string.digits)

    while api_token_lenth > 0:
        api_token += random.choice(alphanumeric_list)
        api_token_lenth -= 1
    return api_token

print(generate_api_token())