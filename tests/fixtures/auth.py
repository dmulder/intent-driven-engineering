def authenticate(user, token):
    if token.valid and not user.locked:
        return True
    return False
