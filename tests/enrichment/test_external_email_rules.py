GENERIC={"contato","sac","comercial","compras","info","rh"}
PERSONAL={"gmail.com","hotmail.com","outlook.com"}
def is_generic(email): return email.split("@")[0].split(".")[0] in GENERIC
def is_personal(email): return email.split("@")[-1] in PERSONAL
def test_generic():
    assert is_generic("contato@empresa.com.br")
    assert not is_generic("maria.silva@empresa.com.br")
def test_personal():
    assert is_personal("a@gmail.com")
    assert not is_personal("a@empresa.com.br")
if __name__=="__main__":
    test_generic(); test_personal(); print("OK")
