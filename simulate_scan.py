# test_registar.py
import time
from Interface import CheckinApp

def main():
    app = CheckinApp()   # cria a app (mas não abre a janela porque não chamamos run())

    # Escolhe um aluno que exista na BD
    student_id = "1071"   # substitui por um número válido

    print("Simular primeira leitura...")
    app._registar(student_id)
  

if __name__ == "__main__":
    main()
