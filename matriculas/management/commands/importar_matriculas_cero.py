import csv
import os
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from matriculas.models import Matricula, MatriculaLaboratorio # Aseguramos la importación del modelo de matrícula
# Asegúrate de importar los modelos FK de sus respectivas apps
from usuarios.models import Estudiante # Asumiendo que Estudiante está en 'usuarios'
from cursos.models import GrupoCurso # Asumiendo que GrupoCurso está en 'cursos'

# Función de ayuda para procesar las notas del CSV
def parse_nota(value):
    """
    Convierte un valor de cadena de nota a float. 
    Retorna None si es nulo, vacío o no es un número válido.
    """
    if value is None or str(value).strip() == '':
        return None
    try:
        # Intentamos convertir a float. Es buena práctica reemplazar comas por puntos.
        return float(str(value).replace(',', '.').strip())
    except ValueError:
        # Si el valor no es numérico, lo tratamos como None (NULL)
        return None

class Command(BaseCommand):
    help = 'Importa registros de matrícula desde un archivo CSV. ELIMINA todos los registros de Matricula existentes antes de importar.'

    def add_arguments(self, parser):
        parser.add_argument(
            'csv_file', 
            type=str, 
            help='La ruta completa al archivo CSV de matrículas'
        )

    def handle(self, *args, **options):
        file_path = options['csv_file']

        if not os.path.exists(file_path):
            raise CommandError(f'El archivo CSV no fue encontrado en: "{file_path}"')

        self.stdout.write(self.style.NOTICE(f'Iniciando importación destructiva de matrículas desde: {file_path}'))
        
        registros_procesados = 0
        
        try:
            with transaction.atomic():
                
                # =======================================================
                # CAMBIO CLAVE: BORRADO MASIVO DE DATOS ANTIGUOS
                # =======================================================
                # 1. Borrar todas las matrículas existentes
                conteo_borrado, _ = Matricula.objects.all().delete()
                # NOTA: Si también quieres borrar MatriculaLaboratorio, haz lo mismo aquí.
                
                self.stdout.write(self.style.WARNING(f"--> Se eliminaron {conteo_borrado} registros de Matricula antes de la importación."))
                # =======================================================
                
                # Lista para la inserción masiva (si aplica)
                matriculas_a_crear = []
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    reader = csv.DictReader(f)
                    
                    for row in reader:
                        estudiante_id = row.get('estudiante_id')
                        grupo_curso_id = row.get('grupo_curso_id')
                        
                        if not estudiante_id or not grupo_curso_id:
                            self.stdout.write(self.style.ERROR(
                                f'Fila omitida por datos faltantes (estudiante_id={estudiante_id}, grupo_curso_id={grupo_curso_id})'
                            ))
                            continue
                            
                        try:
                            # 1. Obtener las instancias de los modelos relacionados (FKs)
                            # Usamos select_related para obtener la información con menos consultas
                            estudiante = Estudiante.objects.select_related('perfil').get(perfil_id=estudiante_id)
                            grupo_curso = GrupoCurso.objects.get(id=grupo_curso_id)

                            # 2. En lugar de update_or_create (que hace 2 consultas/fila), 
                            # vamos a PREPARAR los objetos para bulk_create (1 consulta grande al final).
                            
                            matriculas_a_crear.append(
                                Matricula(
                                    estudiante=estudiante,
                                    grupo_curso=grupo_curso,
                                    estado=True,
                                    EC1=parse_nota(row.get('EC1')),
                                    EP1=parse_nota(row.get('EP1')),
                                    EC2=parse_nota(row.get('EC2')),
                                    EP2=parse_nota(row.get('EP2')),
                                    EC3=parse_nota(row.get('EC3')),
                                    EP3=parse_nota(row.get('EP3')),
                                )
                            )

                            registros_procesados += 1
                            
                        except Estudiante.DoesNotExist:
                            self.stdout.write(self.style.ERROR(
                                f'Error en Estudiante ID {estudiante_id}: No existe el estudiante. Fila omitida.'
                            ))
                        except GrupoCurso.DoesNotExist:
                            self.stdout.write(self.style.ERROR(
                                f'Error en GrupoCurso ID {grupo_curso_id}: No existe el grupo de curso. Fila omitida.'
                            ))
                        except Exception as e:
                             self.stdout.write(self.style.ERROR(
                                f'Error al procesar la fila de Estudiante {estudiante_id}, Grupo {grupo_curso_id}: {e}'
                            ))

                # =======================================================
                # CAMBIO CLAVE: INSERCIÓN MASIVA (bulk_create)
                # Esto es más eficiente que hacer un .save() por objeto.
                # =======================================================
                if matriculas_a_crear:
                    Matricula.objects.bulk_create(matriculas_a_crear)
                    self.stdout.write(self.style.SUCCESS(f'\n--- Inserción Masiva Exitosa ---'))
                    
            self.stdout.write(self.style.SUCCESS(f'\n--- Proceso Finalizado ---'))
            self.stdout.write(self.style.SUCCESS(f'Total de matrículas nuevas creadas: {registros_procesados}'))

        except Exception as e:
            # CommandError detiene el script y notifica que la transacción ha fallado
            raise CommandError(f'Fallo la importación debido a un error de archivo o transacción (Rollback): {e}')