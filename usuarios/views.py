# usuarios/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db.models import Prefetch, Q, F, Case, When, Sum, ExpressionWrapper, DecimalField, FloatField, Value, Avg, Max, Min
from django.db.models import Count
from django.db import transaction
from django.db.models.functions import Coalesce
from django.core.exceptions import ObjectDoesNotExist
from .models import Perfil, Estudiante, Profesor
from cursos.models import Curso, BloqueHorario, GrupoTeoria, GrupoLaboratorio, GrupoCurso
from matriculas.models import Matricula, MatriculaLaboratorio
from reservas.models import Aula, Reserva
from asistencias.models import RegistroAsistencia, RegistroAsistenciaDetalle
import math
from django.utils import timezone
from datetime import datetime, time, timedelta, date
from decimal import Decimal

# ----------------------------------------------------------------------
# 0. VISTAS DE AUTENTICACION
# ----------------------------------------------------------------------

def selector_rol(request):
    """Página inicial para que el usuario elija su rol."""
    ROLES = ['ADMIN', 'SECRETARIA', 'PROFESOR', 'ESTUDIANTE']
    # Si ya está autenticado, redirigir a un dashboard por defecto o usar un template
    if request.session.get('is_authenticated'):
        return redirect('usuarios:dashboard_estudiante') # Redirige a un dashboard seguro
        
    return render(request, 'usuarios/selector_rol.html', {'roles': ROLES})


def login_usuario(request, rol_seleccionado):
    """Maneja la autenticación manual y el inicio de sesión."""
    rol_upper = rol_seleccionado.upper()
    
    if request.method == 'POST':
        email_ingresado = request.POST.get('email')
        password_ingresada = request.POST.get('password')
        
        try:
            # 1. Buscar Perfil por email y rol (Doble verificación)
            usuario_perfil = Perfil.objects.get(
                email=email_ingresado, 
                rol=rol_upper # Crucial: solo autentica si el rol coincide
            )

            # 2. Corroborar contraseña (SIN HASHEO)
            if usuario_perfil.password == password_ingresada:
                
                # 3. Autenticación exitosa: INICIAR SESIÓN MANUALMENTE
                request.session['usuario_id'] = usuario_perfil.id
                request.session['usuario_rol'] = usuario_perfil.rol
                request.session['is_authenticated'] = True 
                
                # 4. Redirigir al dashboard correspondiente
                if usuario_perfil.rol == 'ESTUDIANTE':
                    return redirect('usuarios:dashboard_estudiante')
                if usuario_perfil.rol == 'PROFESOR':
                    return redirect('usuarios:dashboard_profesor')
                if usuario_perfil.rol == 'SECRETARIA':
                    return redirect('usuarios:dashboard_secretaria')
                # ... Lógica para otros roles ...
                
            else:
                messages.error(request, "Contraseña incorrecta.")
                
        except Perfil.DoesNotExist:
            messages.error(request, f"Credenciales inválidas para el rol de {rol_upper}.")
        
    contexto = {'rol': rol_upper}
    return render(request, 'usuarios/login.html', contexto)


def logout_usuario(request):
    """Cierra la sesión del usuario limpiando las variables de sesión."""
    if 'usuario_id' in request.session:
        # Limpia toda la sesión o solo las variables específicas
        request.session.flush() 
    messages.info(request, "Sesión cerrada exitosamente.")
    return redirect('usuarios:selector_rol')

# ----------------------------------------------------------------------
# 1. VISTAS DEL LOS ESTUDIANTES
# ----------------------------------------------------------------------

def check_student_auth(request):
    """Función de ayuda para verificar la sesión y obtener el ID del estudiante."""
    if not request.session.get('is_authenticated') or request.session.get('usuario_rol') != 'ESTUDIANTE':
        messages.warning(request, "Acceso denegado o rol incorrecto.")
        return None, redirect('usuarios:selector_rol')
    
    usuario_id = request.session['usuario_id']
    try:
        estudiante_obj = Estudiante.objects.select_related('perfil').get(perfil__id=usuario_id)
        return estudiante_obj, None
    except Estudiante.DoesNotExist:
        messages.error(request, "Error: Datos de estudiante no encontrados.")
        return None, redirect('usuarios:logout')

def dashboard_estudiante(request):
    estudiante_obj, response = check_student_auth(request)
    if response: 
        return response

    # ----------------------------------------------------------------------
    # 1. Conteo de Cursos y Estado de Laboratorio
    # ----------------------------------------------------------------------
    
    # Cursos del Ciclo (Matrículas de Teoría activas)
    cursos_count = Matricula.objects.filter(
        estudiante=estudiante_obj, 
        estado=True
    ).count()
    
    # Estado de Matrícula Lab.
    tiene_laboratorio = MatriculaLaboratorio.objects.filter(
        estudiante=estudiante_obj
    ).exists()

    # ----------------------------------------------------------------------
    # 2. Cálculo del Promedio Ponderado Global
    # ----------------------------------------------------------------------
    
    # Filtramos matrículas activas y traemos los datos necesarios (notas y créditos)
    matriculas_con_notas = Matricula.objects.filter(
        estudiante=estudiante_obj, 
        estado=True,
        # Filtramos solo las que tienen notas para evitar errores de cálculo en Null
        EC1__isnull=False, EP1__isnull=False, 
        EC2__isnull=False, EP2__isnull=False, 
        EC3__isnull=False, EP3__isnull=False,
    ).select_related('grupo_curso__curso')


    if matriculas_con_notas:
        # A. Calcular la nota final (NF) por cada curso (usando los porcentajes del modelo Curso)
        
        # Primero, calculamos la nota final para cada matrícula (NF = Suma de (Nota * Porcentaje))
        nota_final_expression = ExpressionWrapper(
            # Aseguramos que la operación se haga con Decimales para precisión
            (
                (F('EC1') * F('grupo_curso__curso__porcentajeEC1')) +
                (F('EP1') * F('grupo_curso__curso__porcentajeEP1')) +
                (F('EC2') * F('grupo_curso__curso__porcentajeEC2')) +
                (F('EP2') * F('grupo_curso__curso__porcentajeEP2')) +
                (F('EC3') * F('grupo_curso__curso__porcentajeEC3')) +
                (F('EP3') * F('grupo_curso__curso__porcentajeEP3'))
            ) / 100.0,
            output_field=DecimalField(decimal_places=2)
        )
        
        # Anotamos la Nota Final y el Crédito en el QuerySet
        qs_con_nf = matriculas_con_notas.annotate(
            nota_final=nota_final_expression,
            creditos=F('grupo_curso__curso__creditos')
        )
        
        # B. Calcular el Promedio Ponderado Global (PPG)
        
        # Suma de (NF * Créditos)
        suma_nota_por_creditos = qs_con_nf.aggregate(
            sum_nc=Sum(F('nota_final') * F('creditos'), output_field=DecimalField())
        )['sum_nc']
        
        # Suma Total de Créditos
        total_creditos = qs_con_nf.aggregate(
            sum_c=Sum('creditos', output_field=DecimalField())
        )['sum_c']
        
        promedio_ponderado = None
        if total_creditos and total_creditos > 0:
            promedio_ponderado = suma_nota_por_creditos / total_creditos
            # Redondeamos a dos decimales
            promedio_ponderado = round(promedio_ponderado, 2)
        
    else:
        promedio_ponderado = 'N/A' # O 0.0 si prefieres

    # ----------------------------------------------------------------------
    # 3. Datos Académicos (Faltantes en BD, se simulan o se asumen)
    # ----------------------------------------------------------------------
    # Asumimos que la Carrera y Ciclo están en un modelo de Estudiante o son datos fijos.
    # Como no tenemos un modelo de Carrera, usamos valores de ejemplo.
    
    # ----------------------------------------------------------------------
    # 4. Contexto Final
    # ----------------------------------------------------------------------

    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Dashboard',
        'promedio_ponderado': promedio_ponderado,
        'cursos_count': cursos_count,
        'tiene_laboratorio': tiene_laboratorio,
        # Datos estáticos o supuestos:
        'carrera': 'Ciencia de la Computación',
        #'ciclo_actual': 'VI (Ejemplo)', 
    }
    return render(request, 'usuarios/alumno/dashboard_estudiante.html', contexto)

def mi_cuenta(request):
    estudiante_obj, response = check_student_auth(request)
    if response: return response
    
    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Mi Cuenta',
    }
    return render(request, 'usuarios/alumno/mi_cuenta.html', contexto)

def check_schedule_clash(estudiante_id, nuevo_bloque_horario):
    """
    Verifica si un nuevo BloqueHorario (teórico o laboratorio) causa cruce 
    con el horario existente del estudiante.
    Retorna True si hay cruce, False si no lo hay.
    """
    
    # 1. Obtener todos los bloques de horario actuales del estudiante
    
    # Bloques de Teoría (Matricula)
    teorias_matriculadas = Matricula.objects.filter(estudiante_id=estudiante_id, estado=True)
    grupos_teoria_ids = [m.grupo_curso_id for m in teorias_matriculadas]
    
    # Bloques de Laboratorio (MatriculaLaboratorio)
    labs_matriculados = MatriculaLaboratorio.objects.filter(estudiante_id=estudiante_id)
    grupos_lab_ids = [m.laboratorio_id for m in labs_matriculados]

    # IDs de todos los Grupos (Teoría + Laboratorio)
    todos_grupos_ids = grupos_teoria_ids + grupos_lab_ids
    
    # Consultar todos los bloques de horario activos
    horarios_actuales = BloqueHorario.objects.filter(grupo_curso_id__in=todos_grupos_ids)

    # 2. Iterar y verificar el cruce
    for bloque_actual in horarios_actuales:
        # 2a. Debe ser el mismo día
        if bloque_actual.dia != nuevo_bloque_horario.dia:
            continue

        # 2b. Debe haber solapamiento de tiempo
        # Dos intervalos [A, B] y [C, D] se solapan si A < D y C < B.
        # En nuestro caso, A y C son horaInicio, B y D son horaFin.
        
        # El nuevo bloque termina DESPUÉS de que el actual comienza
        condicion_inicio = nuevo_bloque_horario.horaInicio < bloque_actual.horaFin
        
        # El nuevo bloque comienza ANTES de que el actual termina
        condicion_fin = bloque_actual.horaInicio < nuevo_bloque_horario.horaFin
        
        if condicion_inicio and condicion_fin:
            # Cruce encontrado
            return True, bloque_actual 

    # No se encontró cruce
    return False, None

def matricula_laboratorio(request):
    estudiante_obj, response = check_student_auth(request)
    if response: return response
    
    # --------------------------------------------------------------------------
    # A. LÓGICA DE PROCESAMIENTO (POST) - Matrícula
    # --------------------------------------------------------------------------
    if request.method == 'POST':
        lab_id_a_matricular = request.POST.get('lab_id') # Es el ID del GrupoLaboratorio
        
        if lab_id_a_matricular:
            try:
                # 1. Obtener el grupo de laboratorio
                grupo_lab = GrupoLaboratorio.objects.get(pk=lab_id_a_matricular)
                
                # 2. Verificar existencia de bloques (aunque debería existir)
                bloques_lab = BloqueHorario.objects.filter(grupo_curso_id=grupo_lab.pk)
                
                if not bloques_lab.exists():
                    messages.error(request, "Error: El grupo de laboratorio seleccionado no tiene horario definido.")
                    return redirect('usuarios:matricula_lab_alumno')

                # 3. CRUCE DE HORARIO: Verificar el primer bloque del laboratorio contra el horario del estudiante
                for nuevo_bloque in bloques_lab:
                    hay_cruce, bloque_conflicto = check_schedule_clash(estudiante_obj.pk, nuevo_bloque)
                    
                    if hay_cruce:
                        # Si hay cruce, alertar y no matricular.
                        conflicto_curso = bloque_conflicto.grupo_curso.curso.nombre
                        conflicto_dia = bloque_conflicto.dia
                        conflicto_hora = bloque_conflicto.horaInicio.strftime('%H:%M')
                        
                        messages.error(request, 
                            f"El laboratorio {grupo_lab.grupo_curso.curso.id} ({grupo_lab.grupo_curso.grupo}) choca con tu clase de {conflicto_curso} el {conflicto_dia} a las {conflicto_hora}. Selecciona otro grupo."
                        )
                        # Salimos y volvemos a mostrar la página con la alerta
                        break 
                
                if not hay_cruce:
                    # 4. Si NO HAY CRUCE, proceder a la matrícula
                    MatriculaLaboratorio.objects.create(
                        estudiante=estudiante_obj,
                        laboratorio=grupo_lab
                    )
                    messages.success(request, f"¡Matrícula exitosa! Asignado al laboratorio de {grupo_lab.grupo_curso.curso.nombre} (Grupo {grupo_lab.grupo_curso.grupo}).")
                    
                
            except GrupoLaboratorio.DoesNotExist:
                messages.error(request, "Error: Grupo de laboratorio no encontrado.")
            except Exception as e:
                # Puede ser un error de unicidad (si ya estaba matriculado y no se manejó antes)
                messages.error(request, f"Ocurrió un error al intentar matricular: {e}")

        # Siempre redirigir al GET después de POST para evitar reenvío de formulario
        return redirect('usuarios:matricula_lab_alumno')


    # --------------------------------------------------------------------------
    # B. LÓGICA DE VISUALIZACIÓN (GET) - Opciones Disponibles
    # --------------------------------------------------------------------------

    # 1. Cursos de Teoría en los que el estudiante está matriculado
    matriculas_teoria = Matricula.objects.filter(estudiante=estudiante_obj, estado=True).select_related('grupo_curso__curso')
    cursos_teoria_ids = [m.grupo_curso.curso_id for m in matriculas_teoria]

    # 2. Cursos de Laboratorio ya matriculados
    labs_ya_matriculados = MatriculaLaboratorio.objects.filter(estudiante=estudiante_obj).values_list('laboratorio__grupo_curso__curso_id', flat=True)

    # 3. Opciones de Laboratorio Disponibles
    # Filtramos: Solo laboratorios cuyos cursos coinciden con las teorías matriculadas
    # Excluimos: Cursos de los que ya tiene un laboratorio matriculado
    
    # Prefetch para obtener los bloques y el profesor en una consulta
    bloque_prefetch = Prefetch(
        'grupo_curso__bloquehorario_set',
        queryset=BloqueHorario.objects.select_related('aula').order_by('dia', 'horaInicio')
    )
    
    opciones_laboratorio = GrupoLaboratorio.objects.filter(
        grupo_curso__curso_id__in=cursos_teoria_ids
    ).exclude(
        grupo_curso__curso_id__in=labs_ya_matriculados
    ).select_related(
        'grupo_curso__curso',
        'grupo_curso__profesor__perfil'
    ).prefetch_related(
        bloque_prefetch
    ).order_by(
        'grupo_curso__curso__id', 
        'grupo_curso__grupo'
    )
    
    # 4. Estructurar los datos para el template
    labs_disponibles = []
    
    for lab in opciones_laboratorio:
        # Formatear el horario (puede haber múltiples bloques)
        horarios_formateados = []
        for bloque in lab.grupo_curso.bloquehorario_set.all():
            horarios_formateados.append(
                f"{bloque.get_dia_display()} {bloque.horaInicio.strftime('%H:%M')} - {bloque.horaFin.strftime('%H:%M')}"
            )
            
        labs_disponibles.append({
            'id': lab.pk,
            'codigo_curso': lab.grupo_curso.curso.id,
            'nombre_curso': lab.grupo_curso.curso.nombre,
            'grupo': lab.grupo_curso.grupo,
            'docente': lab.grupo_curso.profesor.perfil.nombre if lab.grupo_curso.profesor else 'Pendiente',
            'horarios': horarios_formateados,
            'aula': lab.grupo_curso.bloquehorario_set.first().aula.id if lab.grupo_curso.bloquehorario_set.first() else 'N/A',
        })

    # 5. Contexto final
    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Matrícula Laboratorio',
        'labs_disponibles': labs_disponibles,
        # Necesitamos saber qué cursos ya tienen laboratorio para informarle al alumno
        'cursos_teoria_sin_lab': [m.grupo_curso.curso_id for m in matriculas_teoria if m.grupo_curso.curso_id not in labs_ya_matriculados],
        # Para mostrar los ya matriculados (opcional, pero útil)
        'labs_matriculados_info': MatriculaLaboratorio.objects.filter(estudiante=estudiante_obj).select_related('laboratorio__grupo_curso__curso', 'laboratorio__grupo_curso__profesor__perfil')
    }
    
    return render(request, 'usuarios/alumno/matricula_lab.html', contexto)

def mis_cursos(request):
    estudiante_obj, response = check_student_auth(request)
    if response: 
        return response

    # ----------------------------------------------------------------------
    # 1. Consulta A: Obtener todas las Matriculas de Laboratorio del estudiante
    # ----------------------------------------------------------------------
    # Usamos .select_related para traer curso y profesor del lab en la misma consulta
    matriculas_lab_map = {}
    
    # labs_del_estudiante es una lista de objetos MatriculaLaboratorio
    labs_del_estudiante = MatriculaLaboratorio.objects.filter(
        estudiante=estudiante_obj
    ).select_related(
        'laboratorio__grupo_curso__curso', 
        'laboratorio__grupo_curso__profesor__perfil'
    )
    
    # Mapeamos {curso_id: info_del_lab} para acceso rápido O(1)
    for mat_lab in labs_del_estudiante:
        curso_id = mat_lab.laboratorio.grupo_curso.curso.id
        matriculas_lab_map[curso_id] = mat_lab
        
    # ----------------------------------------------------------------------
    # 2. Consulta B: Matrículas de Teoría y Estructuración de Datos
    # ----------------------------------------------------------------------
    
    # Obtenemos las matrículas de teoría del estudiante (asumiendo estado=True para activos)
    matriculas_teoria_activas = Matricula.objects.filter(
        estudiante=estudiante_obj, 
        estado=True
    ).select_related(
        'grupo_curso__curso', 
        'grupo_curso__profesor__perfil'
    ).order_by(
        'grupo_curso__curso__nombre'
    )

    cursos_info = []
    
    for mat_teoria in matriculas_teoria_activas:
        grupo_teoria = mat_teoria.grupo_curso
        curso = grupo_teoria.curso
        
        # Intentamos obtener la info del laboratorio usando el mapa
        mat_lab = matriculas_lab_map.get(curso.id)
        
        grupo_lab_info = {
            'grupo': 'N/A', 
            'profesor': 'N/A',
            'id': None
        }
        
        if mat_lab:
            grupo_lab = mat_lab.laboratorio.grupo_curso
            
            grupo_lab_info['grupo'] = grupo_lab.grupo
            grupo_lab_info['id'] = grupo_lab.id
            # Manejo de Profesor de Lab
            profesor_lab_nombre = grupo_lab.profesor.perfil.nombre if grupo_lab.profesor else 'Pendiente'
            grupo_lab_info['profesor'] = profesor_lab_nombre

        # Manejo de Profesor de Teoría
        profesor_teoria_nombre = grupo_teoria.profesor.perfil.nombre if grupo_teoria.profesor else 'Pendiente'
        
        cursos_info.append({
            'curso_id': curso.id,
            'nombre_curso': curso.nombre,
            'grupo_teoria': grupo_teoria.grupo,
            'profesor_teoria': profesor_teoria_nombre,
            'grupo_lab': grupo_lab_info['grupo'],
            'profesor_lab': grupo_lab_info['profesor'],
            'grupo_lab_id': grupo_lab_info['id'], 
        })

    # ----------------------------------------------------------------------
    # 3. Contexto Final
    # ----------------------------------------------------------------------
    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Mis Cursos',
        'cursos_info': cursos_info, 
    }
    return render(request, 'usuarios/alumno/mis_cursos.html', contexto)

def mis_horarios(request):
    estudiante_obj, response = check_student_auth(request)
    if response: 
        return response

    # 1. Definición de la estructura de días
    DIAS_MAP = {
        'LUNES': 'Lunes', 'MARTES': 'Martes', 'MIERCOLES': 'Miércoles', 
        'JUEVES': 'Jueves', 'VIERNES': 'Viernes'
    }
    DIAS = list(DIAS_MAP.values()) 
    
    # Colores (Generador cíclico)
    COLOR_OPTIONS = ['bg-primary', 'bg-warning', 'bg-success', 'bg-danger', 'bg-info', 'bg-secondary', 'bg-dark']
    curso_colores = {} 

    # 2. CONSULTA MODIFICADA: Incluir Laboratorios y Teoría
    
    # a. Obtener IDs de Grupos de Teoría (usando Matricula)
    matriculas_teoria = Matricula.objects.filter(estudiante=estudiante_obj, estado=True) 
    grupos_teoria_ids = [m.grupo_curso_id for m in matriculas_teoria]
    
    # b. Obtener IDs de Grupos de Laboratorio (usando MatriculaLaboratorio)
    # El FK 'laboratorio' en MatriculaLaboratorio es a GrupoLaboratorio, 
    # cuya PK es la misma que la de GrupoCurso.
    matriculas_lab = MatriculaLaboratorio.objects.filter(estudiante=estudiante_obj)
    grupos_lab_ids = [m.laboratorio_id for m in matriculas_lab]
    
    # c. Unificar IDs de todos los grupos matriculados
    todos_grupos_ids = list(set(grupos_teoria_ids + grupos_lab_ids))

    horario_clases = BloqueHorario.objects.filter(
        grupo_curso_id__in=todos_grupos_ids
    ).select_related('grupo_curso__curso', 'aula').order_by('horaInicio') 

    # Si no hay clases
    if not horario_clases:
        contexto = {
            'perfil': estudiante_obj.perfil, 'titulo': 'Mi Horario de Clases',
            'dias': DIAS, 'horas': [], 'horario_data': [], 'curso_colores': {}, 
        }
        return render(request, 'usuarios/alumno/mis_horarios.html', contexto)

    # 3. Generación de Puntos de Corte
    puntos_corte = set()
    for bloque in horario_clases:
        puntos_corte.add(bloque.horaInicio.replace(second=0, microsecond=0))
        puntos_corte.add(bloque.horaFin.replace(second=0, microsecond=0))

    dummy_date = datetime.today().date()
    puntos_corte_dt = sorted(list(set([datetime.combine(dummy_date, t) for t in puntos_corte])))

    horas_data = [] 
    hora_actual = None 
    color_index = 0
    
    # 🚨 BANDERA DE DEPURACIÓN
    horario_con_conflicto = False 

    # 4. Construcción y Consolidación de Filas
    for dt_siguiente in puntos_corte_dt:
        if hora_actual is None:
            hora_actual = dt_siguiente
            continue
        
        dt_inicio_fila = hora_actual
        dt_fin_fila = dt_siguiente
        
        if dt_inicio_fila >= dt_fin_fila:
            hora_actual = dt_siguiente
            continue
            
        hora_inicio_str = dt_inicio_fila.strftime("%H:%M")
        hora_fin_str = dt_fin_fila.strftime("%H:%M")
        rango_hora_str = f"{hora_inicio_str} - {hora_fin_str}"
        
        fila_data = [None] * len(DIAS)
        hay_clase_en_fila = False
        
        # Iterar por día para buscar clases
        for dia_index, dia_key in enumerate(DIAS_MAP.keys()):
            
            clashes_in_slot = [] # Lista para detectar choques
            
            # Buscar la clase que está ACTIVA en este intervalo [dt_inicio_fila, dt_fin_fila]
            for bloque in horario_clases:
                
                b_inicio = bloque.horaInicio.replace(second=0, microsecond=0)
                b_fin = bloque.horaFin.replace(second=0, microsecond=0)
                
                dt_b_inicio = datetime.combine(dummy_date, b_inicio)
                dt_b_fin = datetime.combine(dummy_date, b_fin)
                
                # 🚨 CRITERIO CLAVE CORREGIDO: Solapamiento de tiempo para mostrar bloques largos.
                # La clase está activa si: 
                # 1. Es el día correcto.
                # 2. El inicio de la clase es ANTES O EN el inicio del segmento de la fila.
                # 3. El fin de la clase es DESPUÉS del inicio del segmento de la fila.
                if (bloque.dia == dia_key and 
                    dt_b_inicio <= dt_inicio_fila and 
                    dt_b_fin > dt_inicio_fila): 
                    
                    clashes_in_slot.append(bloque)
            
            
            # 🚨 LÓGICA DE DETECCIÓN DE CONFLICTO
            if len(clashes_in_slot) > 1:
                horario_con_conflicto = True
                clash_names = ", ".join([
                    f"{c.grupo_curso.curso.nombre} ({c.grupo_curso.grupo})" 
                    for c in clashes_in_slot
                ])
                print(f"=========================================================================")
                print(f"⚠️ ¡CHOQUE DE HORARIO DETECTADO! Día: {dia_key}, Horario: {rango_hora_str}")
                print(f"Clases en conflicto: {clash_names}")
                print(f"=========================================================================")

            # Si hay al menos un bloque, lo usamos para llenar la celda
            if clashes_in_slot:
                # Usamos el primer bloque (clashes_in_slot[0]) para llenar la celda
                bloque = clashes_in_slot[0]
                
                curso_obj = bloque.grupo_curso.curso
                
                # Asignación de color (por curso_id)
                if curso_obj.id not in curso_colores:
                    curso_colores[curso_obj.id] = COLOR_OPTIONS[color_index % len(COLOR_OPTIONS)]
                    color_index += 1
                color = curso_colores[curso_obj.id]
                
                # Determinar si es Laboratorio o Teoría (basado en el tipo de aula)
                # NOTA: Esto asume que todas las Aulas de tipo 'LABORATORIO' solo contienen bloques de laboratorio.
                es_laboratorio = (bloque.aula.tipo.upper() == 'LABORATORIO')
                tipo_clase = "LAB" if es_laboratorio else "TEO"
                grupo_nombre = bloque.grupo_curso.grupo
                
                fila_data[dia_index] = {
                    'codigo': str(curso_obj.id), 
                    # El código del grupo mostrará el tipo: Ej: INF101-A (TEO) o INF101-LA (LAB)
                    'codigo_grupo': f"{curso_obj.id}-{grupo_nombre}", 
                    'nombre': curso_obj.nombre,
                    'tipo': tipo_clase,
                    'aula_id': bloque.aula.id, 
                    'color': color,
                }
                hay_clase_en_fila = True
            else:
                # Si la celda está vacía
                pass 
            
        # 5. Consolidación de Filas Vacías (Tiempo Libre)
        if not hay_clase_en_fila and horas_data and horas_data[-1]['tipo'] == 'LIBRE':
            fila_anterior = horas_data[-1]
            fila_anterior['rango'] = f"{fila_anterior['rango'].split(' - ')[0]} - {hora_fin_str}"
        else:
            horas_data.append({
                'rango': rango_hora_str,
                'data': fila_data,
                'tipo': 'CLASE' if hay_clase_en_fila else 'LIBRE',
            })
            
        hora_actual = dt_siguiente 

    # 🚨 MENSAJE FINAL DE DEPURACIÓN
    print("\n--- RESUMEN DE HORARIO ---")
    if horario_con_conflicto:
        print("🔴 Se detectaron CHOQUES DE HORARIO en los bloques anteriores. Revisar la asignación de clases.")
    else:
        print("🟢 El horario se procesó correctamente. No se detectaron choques.")
    print("--------------------------\n")

    # 6. Preparar contexto final
    horario_data_final = [f['data'] for f in horas_data]
    horas_rango_final = [f['rango'] for f in horas_data]
    
    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Mi Horario de Clases',
        'dias': DIAS,
        'horas': horas_rango_final, 
        'horario_data': horario_data_final, 
        'curso_colores': curso_colores, 
    }
    return render(request, 'usuarios/alumno/mis_horarios.html', contexto)

def mis_notas(request):
    # Asumo que check_student_auth devuelve el objeto Estudiante autenticado
    estudiante_obj, response = check_student_auth(request)
    if response: return response

    # 1. Obtener todas las matrículas del estudiante con información relacionada
    matriculas = Matricula.objects.filter(
        estudiante=estudiante_obj
    ).select_related(
        'grupo_curso__curso', 
        'grupo_curso__profesor__perfil'
    )
    
    # Lista de cursos matriculados para el selector (Dropdown)
    cursos_matriculados = []
    for m in matriculas:
        cursos_matriculados.append({
            'curso_id': m.grupo_curso.curso.id,
            'nombre_curso': m.grupo_curso.curso.nombre,
        })
    
    # Inicializar datos para el detalle de notas
    curso_seleccionado_data = None
    curso_id_seleccionado = request.GET.get('curso')

    if curso_id_seleccionado:
        try:
            # Buscar la matrícula específica para el curso seleccionado
            matricula_seleccionada = matriculas.get(
                grupo_curso__curso__id=curso_id_seleccionado
            )
            grupo_curso = matricula_seleccionada.grupo_curso
            curso_obj = grupo_curso.curso
            profesor_obj = grupo_curso.profesor
            
            # 2. Definir las evaluaciones
            evaluaciones = [
                {'nombre': 'Examen Parcial 1 (EP1)', 'nota_field': 'EP1', 'porcentaje_field': 'porcentajeEP1'},
                {'nombre': 'Nota Continua 1 (EC1)', 'nota_field': 'EC1', 'porcentaje_field': 'porcentajeEC1'},
                {'nombre': 'Examen Parcial 2 (EP2)', 'nota_field': 'EP2', 'porcentaje_field': 'porcentajeEP2'},
                {'nombre': 'Nota Continua 2 (EC2)', 'nota_field': 'EC2', 'porcentaje_field': 'porcentajeEC2'},
                {'nombre': 'Examen Parcial 3 (EP3)', 'nota_field': 'EP3', 'porcentaje_field': 'porcentajeEP3'},
                {'nombre': 'Nota Continua 3 (EC3)', 'nota_field': 'EC3', 'porcentaje_field': 'porcentajeEC3'},
            ]
            
            notas_detalle = []
            promedio_final = 0.0
            total_porcentaje = 0
            
            # === VARIABLES DE CÁLCULO DE META ===
            TARGET_PASSING_SCORE = 10.5
            MAX_GRADE = 20.0
            TARGET_WEIGHTED_SUM = TARGET_PASSING_SCORE * 100 # 1050
            
            current_weighted_score = 0.0 # Suma de (Nota * Porcentaje) de notas registradas
            total_weight_missing = 0.0   # Suma de Porcentaje de notas faltantes
            missing_evaluations_count = 0
            
            missing_notes_list = [] 
            # ===========================================
            
            # 3. Procesar y calcular el promedio ponderado y métricas de notas faltantes
            for eval_data in evaluaciones:
                nota = getattr(matricula_seleccionada, eval_data['nota_field'])
                porcentaje = getattr(curso_obj, eval_data['porcentaje_field'])

                if porcentaje is not None and porcentaje > 0:
                    
                    notas_detalle.append({
                        'nombre': eval_data['nombre'],
                        'peso': porcentaje,
                        'nota': nota if nota is not None else 'N/A' 
                    })
                    
                    if nota is not None:
                        current_weighted_score += (nota * porcentaje)
                        promedio_final += (nota * porcentaje) / 100
                        
                    else:
                        total_weight_missing += porcentaje
                        missing_evaluations_count += 1
                        
                        missing_notes_list.append({
                            'name': eval_data['nombre'],
                            'weight': porcentaje,
                            'field': eval_data['nota_field']
                        })
                        
                    total_porcentaje += porcentaje

            # === 4. CÁLCULO DE LA NOTA MÍNIMA REQUERIDA Y OPCIONES ===
            required_avg_grade = None
            is_impossible = False
            required_weighted_sum = 0.0 
            approval_scenarios = [] 

            if missing_evaluations_count > 0:
                required_weighted_sum = TARGET_WEIGHTED_SUM - current_weighted_score
                
                if required_weighted_sum <= 0:
                    required_avg_grade = 0.0 
                else:
                    required_avg_grade = required_weighted_sum / total_weight_missing
                    
                    if required_avg_grade > MAX_GRADE:
                        is_impossible = True
                        
                # 4.2. GENERAR ESCENARIOS DE APROBACIÓN (solo si faltan al menos 2 notas)
                if missing_evaluations_count >= 2 and required_weighted_sum > 0:
                    
                    # Ordenar por peso descendente para tomar las 2 notas más importantes
                    missing_notes_list.sort(key=lambda x: x['weight'], reverse=True)
                    
                    top_missing_note_1 = missing_notes_list[0]
                    top_missing_note_2 = missing_notes_list[1]
                    
                    P1 = top_missing_note_1['weight']
                    P2 = top_missing_note_2['weight']
                    
                    # El resto del peso (si hay más de 2 notas faltantes) se asume como 0
                    P_rest = total_weight_missing - P1 - P2
                    required_weighted_sum_for_P1_P2 = required_weighted_sum 

                    def calculate_scenario(N1_grade, P1, P2, required_sum, current_score_sum):
                        """Calcula N2, el promedio final y verifica la validez."""
                        N1_contribution = N1_grade * P1
                        required_by_N2 = required_sum - N1_contribution
                        
                        N2_grade = required_by_N2 / P2 if P2 > 0 else MAX_GRADE + 1

                        # Redondear N2 al entero más cercano hacia arriba (math.ceil)
                        N2_grade_rounded = max(0.0, math.ceil(min(MAX_GRADE, N2_grade)))
                        
                        # Promedio Final Real con esta combinación (asumiendo 0s en las demás faltantes)
                        final_weighted_score = current_score_sum + (N1_grade * P1) + (N2_grade_rounded * P2)
                        final_average = final_weighted_score / 100.0
                        
                        return {
                            'N1_grade': round(N1_grade),
                            'N2_grade': N2_grade_rounded,
                            'final_average': round(final_average, 1),
                            'is_passing': final_average >= TARGET_PASSING_SCORE
                        }
                    
                    # --- Generación de Escenarios Clave ---
                    
                    # 1. ESCENARIO BAJO: N1 (la más pesada) saca 11
                    N1_1 = 11.0 
                    scenario_1 = calculate_scenario(N1_1, P1, P2, required_weighted_sum_for_P1_P2, current_weighted_score)
                    if scenario_1['is_passing']:
                         approval_scenarios.append({**scenario_1, 'N1_name': top_missing_note_1['name'], 'N2_name': top_missing_note_2['name'], 'N1_weight': P1, 'N2_weight': P2})
                    
                    # 2. ESCENARIO MEDIO: N1 saca un promedio (15)
                    N1_2 = 15.0
                    scenario_2 = calculate_scenario(N1_2, P1, P2, required_weighted_sum_for_P1_P2, current_weighted_score)
                    if scenario_2['is_passing']:
                         approval_scenarios.append({**scenario_2, 'N1_name': top_missing_note_1['name'], 'N2_name': top_missing_note_2['name'], 'N1_weight': P1, 'N2_weight': P2})
                    
                    # 3. ESCENARIO ALTO: N1 saca 20
                    N1_3 = 20.0
                    scenario_3 = calculate_scenario(N1_3, P1, P2, required_weighted_sum_for_P1_P2, current_weighted_score)
                    if scenario_3['is_passing']:
                         approval_scenarios.append({**scenario_3, 'N1_name': top_missing_note_1['name'], 'N2_name': top_missing_note_2['name'], 'N1_weight': P1, 'N2_weight': P2})

                    
                    # Eliminar duplicados si las notas resultantes son iguales (ej: si N2 da 0 en dos escenarios)
                    unique_scenarios = []
                    seen = set()
                    for s in approval_scenarios:
                        key = (s['N1_grade'], s['N2_grade'], s['final_average'])
                        if key not in seen:
                            seen.add(key)
                            unique_scenarios.append(s)
                            
                    # Ordenar por N1 grade (de menor a mayor) para un flujo lógico
                    approval_scenarios = sorted(unique_scenarios, key=lambda x: x['N1_grade'])
                    
                    # Si no se encontró ningún escenario viable (incluso con N1=20), significa que es imposible, pero debemos forzar el escenario de 20s.
                    if not approval_scenarios and is_impossible:
                         # Solo se mostrará este escenario de "puros 20s"
                        final_weighted_score_max = current_weighted_score + (total_weight_missing * MAX_GRADE)
                        final_average_max = final_weighted_score_max / 100.0
                        
                        approval_scenarios.append({
                            'N1_grade': round(MAX_GRADE),
                            'N2_grade': round(MAX_GRADE),
                            'N1_name': top_missing_note_1['name'], 
                            'N2_name': top_missing_note_2['name'],
                            'N1_weight': P1,
                            'N2_weight': P2,
                            'final_average': round(final_average_max, 1),
                            'is_passing': False
                        })
                        
                # Caso especial: Si solo falta 1 nota, no se generan combinaciones, el template usará required_avg_grade.


            # 5. Preparar el objeto de contexto para el detalle del curso
            profesor_nombre = profesor_obj.perfil.nombre if profesor_obj else 'No Asignado'
            
            curso_seleccionado_data = {
                'id': curso_obj.id,
                'nombre': curso_obj.nombre,
                'grupo': grupo_curso.grupo,
                'profesor': profesor_nombre,
                'notas': notas_detalle,
                'promedio_final': round(promedio_final, 1) if promedio_final is not None else None, 
                'total_porcentaje': total_porcentaje,
                # Variables AÑADIDAS para el template
                'required_avg_grade': required_avg_grade,
                'missing_evaluations_count': missing_evaluations_count,
                'total_weight_missing': total_weight_missing,
                'is_impossible': is_impossible,
                'required_weighted_sum': required_weighted_sum,
                'approval_scenarios': approval_scenarios
            }

        except Matricula.DoesNotExist:
            pass
        except Exception as e:
            # Puedes usar logging.error(f"Error procesando notas: {e}", exc_info=True)
            print(f"Error procesando notas: {e}") 
            pass


    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Mis Notas',
        'cursos_matriculados': cursos_matriculados,
        'curso_seleccionado_data': curso_seleccionado_data,
        'curso_id_seleccionado': curso_id_seleccionado
    }
    return render(request, 'usuarios/alumno/mis_notas.html', contexto)
        
# ----------------------------------------------------------------------
# 2. VISTAS DEL PROFESOR
# ----------------------------------------------------------------------

def check_professor_auth(request):
    """Función de ayuda para verificar la sesión y obtener el ID del profesor."""
    # 1. Verificar autenticación y rol
    if not request.session.get('is_authenticated') or request.session.get('usuario_rol') != 'PROFESOR':
        messages.warning(request, "Acceso denegado o rol incorrecto.")
        return None, redirect('usuarios:selector_rol')
    
    usuario_id = request.session['usuario_id']
    try:
        # 2. Obtener el objeto Profesor y su Perfil asociado
        profesor_obj = Profesor.objects.select_related('perfil').get(perfil__id=usuario_id)
        return profesor_obj, None
    except Profesor.DoesNotExist:
        messages.error(request, "Error: Datos de profesor no encontrados. Cierre de sesión forzado.")
        return None, redirect('usuarios:logout')

def dashboard_profesor(request):
    """Muestra la página de inicio del profesor con datos dinámicos."""
    profesor_obj, response = check_professor_auth(request)
    if response:
        return response
    
    contexto = {
        'perfil': profesor_obj.perfil,
        'profesor': profesor_obj,
        'titulo': 'Inicio - Panel Docente',
    }
    # Ruta completa a la plantilla dentro de la subcarpeta 'profesor/'
    return render(request, 'usuarios/profesor/dashboard_profesor.html', contexto)


def mi_cuenta_profesor(request):
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    contexto = {'perfil': profesor_obj.perfil, 'titulo': 'Mi Cuenta'}
    return render(request, 'usuarios/profesor/mi_cuenta_profesor.html', contexto)

def mis_cursos_profesor(request):
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    
    # 1. Obtener la Carga Académica (SIN ANOTACIÓN DE CONTEO)
    teoria_groups = GrupoTeoria.objects.all()
    laboratorio_groups = GrupoLaboratorio.objects.all()

    carga_academica = GrupoCurso.objects.filter(
        profesor=profesor_obj
    ).select_related(
        'curso'
    ).prefetch_related(
        Prefetch('grupoteoria', queryset=teoria_groups, to_attr='es_teoria'), 
        Prefetch('grupolaboratorio', queryset=laboratorio_groups, to_attr='es_laboratorio') 
    ).order_by(
        'curso__id', 'grupo'
    )
    
    periodo_actual = "2025-II" 

    # 2. Procesar la información para añadir el 'tipo' de curso
    cursos_procesados = []
    for grupo in carga_academica:
        
        tipo_curso = "Desconocido"
        if grupo.es_teoria:
            tipo_curso = "Teoría"
        elif grupo.es_laboratorio:
            tipo_curso = "Laboratorio"
        
        grupo.tipo = tipo_curso
        cursos_procesados.append(grupo)


    # 3. Pasar los datos al contexto
    contexto = {
        'perfil': profesor_obj.perfil, 
        'titulo': 'Carga Académica',
        'carga_academica': cursos_procesados,
        'periodo': periodo_actual,
    }
    
    return render(request, 'usuarios/profesor/mis_cursos_profesor.html', contexto)

def horarios_profesor(request):
    """
    Muestra el horario de clases de un profesor consolidando los bloques 
    horarios y detectando posibles conflictos de tiempo.
    """
    # --- 0. Manejo de Autenticación ---
    # La función auxiliar verifica si el usuario es un profesor válido.
    profesor_obj, response = check_professor_auth(request)
    
    # Si 'response' no es None, significa que hubo un error de auth o una redirección.
    if response: 
        return response

    # 1. Definición de la estructura de días (mapeo y lista ordenada)
    DIAS_MAP = {
        'LUNES': 'Lunes', 'MARTES': 'Martes', 'MIERCOLES': 'Miércoles', 
        'JUEVES': 'Jueves', 'VIERNES': 'Viernes'
    }
    DIAS = list(DIAS_MAP.values()) # Lista de nombres de días para las cabeceras de la tabla
    
    # Colores (Generador cíclico)
    COLOR_OPTIONS = ['bg-primary', 'bg-warning', 'bg-success', 'bg-danger', 'bg-info', 'bg-secondary', 'bg-dark']
    curso_colores = {} # Diccionario para almacenar el color asignado a cada curso (por ID)
    cursos_en_horario = {} # Variable para almacenar el ID y el nombre del curso (para la leyenda)
    color_index = 0

    # 2. CONSULTA: Obtener todos los grupos (Teoría y Laboratorio) asignados al Profesor
    # Usamos .filter() para obtener los grupos asignados a este profesor
    grupos_asignados = GrupoCurso.objects.filter(profesor=profesor_obj) 
    grupos_asignados_ids = [g.id for g in grupos_asignados]
    
    # 3. Obtener los Bloques de Horario para esos grupos
    # Usamos select_related para optimizar las consultas a la base de datos
    horario_clases = BloqueHorario.objects.filter(
        grupo_curso_id__in=grupos_asignados_ids
    ).select_related('grupo_curso__curso', 'aula').order_by('horaInicio') 

    # Si no hay clases
    if not horario_clases:
        contexto = {
            'perfil': profesor_obj.perfil, 
            'titulo': 'Mi Horario de Clases',
            'dias': DIAS, 
            'horario_consolidado': [], 
            'leyenda_cursos': [], 
            'horario_con_conflicto': False,
        }
        return render(request, 'usuarios/profesor/horarios_profesor.html', contexto)

    # 4. Generación de Puntos de Corte (Todas las horas de inicio y fin únicas)
    puntos_corte = set()
    for bloque in horario_clases:
        # Añadir hora de inicio y fin (sin segundos/microsegundos para la tabla)
        puntos_corte.add(bloque.horaInicio.replace(second=0, microsecond=0))
        puntos_corte.add(bloque.horaFin.replace(second=0, microsecond=0))

    # Convertir los puntos de corte de 'time' a 'datetime' para facilitar las comparaciones, 
    # usando una fecha ficticia (dummy_date).
    dummy_date = datetime.today().date()
    puntos_corte_dt = sorted(list(set([datetime.combine(dummy_date, t) for t in puntos_corte])))
    
    # Variables de estado
    horario_consolidado = [] # Almacenará las filas de la tabla
    hora_actual = None # Marca de tiempo inicial para la primera fila
    horario_con_conflicto = False # Flag para la alerta

    # 5. Construcción y Consolidación de Filas
    # Iteramos sobre los puntos de corte para crear los intervalos de tiempo de la tabla
    for dt_siguiente in puntos_corte_dt:
        if hora_actual is None:
            hora_actual = dt_siguiente
            continue
        
        dt_inicio_fila = hora_actual
        dt_fin_fila = dt_siguiente
        
        # Saltamos si el intervalo es cero o negativo
        if dt_inicio_fila >= dt_fin_fila:
            hora_actual = dt_siguiente
            continue
            
        # Formato de rango de hora para la fila
        hora_inicio_str = dt_inicio_fila.strftime("%H:%M")
        hora_fin_str = dt_fin_fila.strftime("%H:%M")
        rango_hora_str = f"{hora_inicio_str} - {hora_fin_str}"
        
        fila_data = [None] * len(DIAS) # Datos de la celda para cada día de la semana
        hay_clase_en_fila = False
        
        # Iterar por día para buscar clases que caigan en este intervalo [dt_inicio_fila, dt_fin_fila)
        for dia_index, dia_key in enumerate(DIAS_MAP.keys()):
            
            clashes_in_slot = [] # Lista para detectar conflictos en esta celda (día/hora)
            
            for bloque in horario_clases:
                
                b_inicio = bloque.horaInicio.replace(second=0, microsecond=0)
                b_fin = bloque.horaFin.replace(second=0, microsecond=0)
                
                dt_b_inicio = datetime.combine(dummy_date, b_inicio)
                dt_b_fin = datetime.combine(dummy_date, b_fin)
                
                # CRITERIO CLAVE: La clase debe:
                # 1. Ser en el día correcto
                # 2. Su inicio debe ser menor o igual al inicio de la fila
                # 3. Su fin debe ser MAYOR al inicio de la fila (garantiza que la clase cubre el inicio del intervalo)
                if (bloque.dia == dia_key and 
                    dt_b_inicio <= dt_inicio_fila and 
                    dt_b_fin > dt_inicio_fila): 
                        
                    clashes_in_slot.append(bloque)
            
            
            # Detección y registro de CONFLICTO
            if len(clashes_in_slot) > 1:
                horario_con_conflicto = True
                # Solo para fines de depuración/logging
                clash_names = ", ".join([
                    f"{c.grupo_curso.curso.nombre} (G-{c.grupo_curso.grupo})" 
                    for c in clashes_in_slot
                ])
                print(f"=========================================================================")
                print(f"⚠️ ¡CHOQUE DE HORARIO DOCENTE! Día: {dia_key}, Horario: {rango_hora_str}")
                print(f"Clases en conflicto: {clash_names}")
                print(f"=========================================================================")

            # Si hay al menos un bloque, usamos el primero para llenar la celda
            if clashes_in_slot:
                bloque = clashes_in_slot[0]
                curso_obj = bloque.grupo_curso.curso
                
                # Asignación de color (por curso_id) y creación de entrada de leyenda
                if curso_obj.id not in curso_colores:
                    color = COLOR_OPTIONS[color_index % len(COLOR_OPTIONS)]
                    curso_colores[curso_obj.id] = color
                    cursos_en_horario[curso_obj.id] = {'nombre': curso_obj.nombre, 'color': color}
                    color_index += 1
                
                color = curso_colores[curso_obj.id]
                
                # Determinar si es Laboratorio o Teoría (buscando el OneToOneField)
                tipo_clase = "TEO"
                try:
                    # Intenta acceder al objeto relacionado inverso 'grupoteoria'
                    # Esto solo existe si es un GrupoTeoria
                    _ = bloque.grupo_curso.grupoteoria 
                except GrupoTeoria.DoesNotExist:
                    # Si falla, es un grupo de Laboratorio (o al menos no es Teoría)
                    tipo_clase = "LAB" 
                
                grupo_nombre = bloque.grupo_curso.grupo
                
                # Datos de la celda de la tabla
                fila_data[dia_index] = {
                    'codigo': str(curso_obj.id), 
                    'codigo_grupo': f"{curso_obj.id}-{grupo_nombre}", 
                    'nombre': curso_obj.nombre,
                    'tipo': tipo_clase,
                    'aula_nombre': bloque.aula.id, 
                    'color': color,
                    # Flag para el template si hay conflicto en la celda
                    'conflicto': len(clashes_in_slot) > 1, 
                }
                hay_clase_en_fila = True
            
        # 6. Consolidación de Filas Vacías (Tiempo Libre)
        # Esto agrupa bloques 'LIBRE' adyacentes en una sola fila para que el horario no sea demasiado largo.
        if not hay_clase_en_fila and horario_consolidado and horario_consolidado[-1]['tipo'] == 'LIBRE':
            fila_anterior = horario_consolidado[-1]
            # Actualiza el rango de la fila anterior para que termine en la hora_fin_str actual
            fila_anterior['rango'] = f"{fila_anterior['rango'].split(' - ')[0]} - {hora_fin_str}"
        else:
            # Crea una nueva fila para Clase o un nuevo bloque Libre
            horario_consolidado.append({
                'rango': rango_hora_str,
                'data': fila_data,
                'tipo': 'CLASE' if hay_clase_en_fila else 'LIBRE',
            })
            
        hora_actual = dt_siguiente # Mueve el marcador de tiempo para la siguiente iteración

    # 7. Preparar contexto final
    leyenda_cursos = list(cursos_en_horario.values())
    
    contexto = {
        'perfil': profesor_obj.perfil,
        'titulo': 'Mi Horario de Clases',
        'dias': DIAS,
        'horario_consolidado': horario_consolidado, 
        'leyenda_cursos': leyenda_cursos, 
        'horario_con_conflicto': horario_con_conflicto, # Para mostrar la alerta en el template
    }
    return render(request, 'usuarios/profesor/horarios_profesor.html', contexto)

def acreditacion(request):
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    contexto = {'perfil': profesor_obj.perfil, 'titulo': 'Acreditación y Documentos'}
    return render(request, 'usuarios/profesor/acreditacion.html', contexto)

def registro_asistencia(request):
    # 1. Autenticación
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    
    # Valores predeterminados (GET)
    grupo_id_url = request.GET.get('grupo')
    fecha_url = request.GET.get('fecha')
    
    grupo_seleccionado = None
    estudiantes_matriculados = []
    asistencia_guardada = None
    historial_fechas = []
    
    # Formatear la fecha actual para el input date si no hay selección
    fecha_actual_str = date.today().isoformat() 

    # 1.1. Obtener la Carga Académica (para el selector de grupos)
    grupos_asignados = GrupoCurso.objects.filter(
        profesor=profesor_obj
    ).select_related('curso').order_by('curso__nombre', 'grupo')
    
    # -----------------------------------------------------------
    # 2. Manejo de la subida de asistencia (POST)
    # -----------------------------------------------------------
    if request.method == 'POST':
        grupo_id_post = request.POST.get('grupo_id')
        fecha_post_str = request.POST.get('fecha_sesion')
        
        if not grupo_id_post or not fecha_post_str:
            messages.error(request, "Error: Faltan datos esenciales (Grupo o Fecha de Sesión).")
            return redirect('registro_asistencia') # Redirige a la página base
        
        try:
            grupo_obj = GrupoCurso.objects.get(id=grupo_id_post, profesor=profesor_obj)
            fecha_post = datetime.strptime(fecha_post_str, '%Y-%m-%d').date()
            
            # 2.1. Verificar si ya existe un registro de asistencia para esta fecha y grupo
            if RegistroAsistencia.objects.filter(grupo_curso=grupo_obj, fechaClase=fecha_post).exists():
                messages.warning(request, f"Ya existe un registro de asistencia para el grupo {grupo_obj.id} en la fecha {fecha_post_str}. ¡Se actualizará!")
                registro_existente = RegistroAsistencia.objects.get(grupo_curso=grupo_obj, fechaClase=fecha_post)
            else:
                # 2.2. Crear el nuevo registro principal (RegistroAsistencia)
                registro_asistencia_new = RegistroAsistencia.objects.create(
                    grupo_curso=grupo_obj,
                    ipProfesor=request.META.get('REMOTE_ADDR', '127.0.0.1'),
                    fechaClase=fecha_post,
                    # OJO: La hora de inicio de ventana debería ser dinámica, aquí usamos la actual
                    horaInicioVentana=timezone.now().time()
                )
                registro_existente = registro_asistencia_new
                
            updated_count = 0
            
            with transaction.atomic():
                # 2.3. Procesar los detalles por estudiante
                for key, value in request.POST.items():
                    if key.startswith('asistencia_'):
                        # key = 'asistencia_CUI'
                        estudiante_cui = key.split('_')[1]
                        
                        try:
                            # Asume que el ID del perfil es el CUI/Código del estudiante
                            estudiante_obj = Estudiante.objects.get(perfil__id=estudiante_cui)
                            
                            # Mapear 'A' y 'F' a los choices del modelo
                            estado_asistencia = 'PRESENTE' if value == 'A' else 'FALTA'
                            
                            # Buscar o crear el detalle de asistencia
                            detalle, created = RegistroAsistenciaDetalle.objects.update_or_create(
                                registro_asistencia=registro_existente,
                                estudiante=estudiante_obj,
                                defaults={'estado': estado_asistencia}
                            )
                            if created or detalle.estado != estado_asistencia:
                                updated_count += 1
                                
                        except Estudiante.DoesNotExist:
                            messages.error(request, f"Estudiante con CUI {estudiante_cui} no encontrado.")
                            continue
            
            if updated_count > 0:
                 messages.success(request, f"Asistencia para {grupo_obj.id} del {fecha_post_str} guardada/actualizada con éxito ({updated_count} registros).")
            else:
                 messages.info(request, "No se detectaron cambios en la asistencia.")

            return redirect(f"{request.path}?grupo={grupo_id_post}&fecha={fecha_post_str}")
        
        except GrupoCurso.DoesNotExist:
            messages.error(request, "Error: El grupo no existe o no está asignado a usted.")
        except Exception as e:
            messages.error(request, f"Error al guardar la asistencia: {e}")
            
        # Si falla, se redirige con los parámetros que se intentaron usar
        return redirect(f"{request.path}?grupo={grupo_id_post}&fecha={fecha_post_str}")

    # -----------------------------------------------------------
    # 3. Manejo de la visualización de datos (GET)
    # -----------------------------------------------------------
    if grupo_id_url and fecha_url:
        try:
            # 3.1. Obtener el grupo seleccionado
            grupo_seleccionado = GrupoCurso.objects.get(id=grupo_id_url, profesor=profesor_obj)
            fecha_sesion = datetime.strptime(fecha_url, '%Y-%m-%d').date()
            
            # 3.2. Obtener Estudiantes Matriculados
            # Asume que la relación inversa de Matricula a GrupoCurso está definida (Matricula.grupo_curso)
            estudiantes_matriculados_qs = Estudiante.objects.filter(
                matricula__grupo_curso=grupo_seleccionado
            ).select_related(
                'perfil'
            ).order_by('perfil__nombre')
            
            # 3.3. Obtener el registro de asistencia si ya existe para precargar
            try:
                asistencia_guardada = RegistroAsistencia.objects.prefetch_related(
                    Prefetch(
                        'registroasistenciadetalle_set', # Nombre de la relación inversa
                        queryset=RegistroAsistenciaDetalle.objects.select_related('estudiante'),
                        to_attr='detalles_asistencia'
                    )
                ).get(grupo_curso=grupo_seleccionado, fechaClase=fecha_sesion)
                
            except RegistroAsistencia.DoesNotExist:
                asistencia_guardada = None
            
            # 3.4. Combinar estudiantes y estado de asistencia
            estudiantes_con_asistencia = []
            asistencia_map = {}
            if asistencia_guardada:
                # Crear un mapa CUI -> Estado para consulta rápida
                asistencia_map = {
                    detalle.estudiante.perfil.id: detalle.estado 
                    for detalle in asistencia_guardada.detalles_asistencia
                }

            for estudiante in estudiantes_matriculados_qs:
                estado = asistencia_map.get(estudiante.perfil.id, 'PRESENTE') # Predeterminado: PRESENTE ('A')
                estudiantes_con_asistencia.append({
                    'cui': estudiante.perfil.id,
                    'nombre': estudiante.perfil.nombre,
                    'estado': 'A' if estado == 'PRESENTE' else 'F'
                })
                
            estudiantes_matriculados = estudiantes_con_asistencia

        except GrupoCurso.DoesNotExist:
            messages.error(request, f"El grupo {grupo_id_url} no existe o no está asignado a usted.")
        except Exception as e:
            messages.error(request, f"Ocurrió un error al cargar los datos: {e}")

    # 3.5. Obtener Historial de Asistencia para el grupo seleccionado
    if grupo_seleccionado:
        historial_fechas = RegistroAsistencia.objects.filter(
            grupo_curso=grupo_seleccionado
        ).values_list('fechaClase', flat=True).order_by('-fechaClase')
    
    # 4. Contexto final para el template
    contexto = {
        'perfil': profesor_obj.perfil, 
        'titulo': 'Registro de Asistencia',
        'grupos_asignados': grupos_asignados,
        'grupo_seleccionado': grupo_seleccionado,
        'grupo_id_url': grupo_id_url,
        'fecha_url': fecha_url if fecha_url else fecha_actual_str, # Se usa para precargar el input date
        'estudiantes_matriculados': estudiantes_matriculados,
        'asistencia_guardada': asistencia_guardada,
        'historial_fechas': historial_fechas,
    }
    return render(request, 'usuarios/profesor/registro_asistencia.html', contexto)

def obtener_bloques_recurrentes_ocupados(request, aula_id_a_filtrar=None):
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    aula_queryset = BloqueHorario.objects
    if aula_id_a_filtrar:
        aula_queryset = aula_queryset.filter(aula__id=aula_id_a_filtrar)

    ocupaciones_aula = aula_queryset.values(
        'dia',
        'horaInicio',
        'horaFin',
        'aula__id'
    ).distinct()

    profesor_id = profesor_obj.perfil.id
    ocupaciones_profesor = BloqueHorario.objects.filter(
        grupo_curso__profesor__perfil__id=profesor_id
    ).values(
        'dia',
        'horaInicio',
        'horaFin',
        'aula__id'
    ).distinct()

    bloques_ocupados_profesor = list(ocupaciones_profesor)
    bloques_ocupados_aula = list(ocupaciones_aula)

    return{
        'ocupaciones_aula': bloques_ocupados_aula,
        'ocupaciones_profesor': bloques_ocupados_profesor,
        'aulas_existentes': list(Aula.objects.values('id','tipo'))
    }

def horarios_reserva (request):
    profesor_obj, response = check_professor_auth(request)
    if response: return response

    COLOR_DISPONIBLE = 'bg-success-subtle'
    COLOR_AULA_OCUPADA = 'bg-danger'
    COLOR_PROFESOR_OCUPADO = 'bg-warning-soft'
    COLOR_MI_RESERVA = 'tw-bg-blue-600 tw-text-white'
    DIAS_MAP = {
        'LUNES': 'Lunes', 'MARTES': 'Martes', 'MIERCOLES': 'Miercoles', 'JUEVES': 'Jueves', 'VIERNES': 'Viernes'
    }

    #calculo rango 2 semanas
    hoy=date.today()
    weekday_hoy = hoy.weekday()
    if weekday_hoy >= 5: # Si es Sábado o Domingo
        # Empezar desde el PRÓXIMO Lunes
        dias_para_lunes = 7 - weekday_hoy
        inicio_semana = hoy + timedelta(days=dias_para_lunes)
    else:
        # Empezar desde el Lunes de ESTA semana
        dias_hasta_lunes = weekday_hoy
        inicio_semana = hoy - timedelta(days=dias_hasta_lunes)
    fin_periodo = inicio_semana + timedelta(weeks=2)

    dias_a_mostrar: list[date] = []
    fecha_actual = inicio_semana
    while fecha_actual < fin_periodo:
        if fecha_actual.weekday() < 5:
            dias_a_mostrar.append(fecha_actual)
        fecha_actual+=timedelta(days=1)
    fin_periodo = dias_a_mostrar[-1] if dias_a_mostrar else hoy

    aula_id_a_filtrar = request.GET.get('aula_id')
    if not aula_id_a_filtrar:
        aula_id_a_filtrar='101'
    
    if not aula_id_a_filtrar:
        reservas_existentes=[]
    else:
        reservas_existentes = Reserva.objects.filter(
            aula__id=aula_id_a_filtrar,
            fecha_reserva__gte=inicio_semana,
            fecha_reserva__lte=fin_periodo
        ).select_related('aula','profesor')

    bloques_ocupados = obtener_bloques_recurrentes_ocupados(request, aula_id_a_filtrar)
    ocupaciones_aulas = bloques_ocupados['ocupaciones_aula']
    ocupaciones_profesor = bloques_ocupados['ocupaciones_profesor']
    aulas_existentes = bloques_ocupados['aulas_existentes']

    DIAS_MAP_WEEKDAY = {
        0: 'LUNES', 1: 'MARTES', 2: 'MIERCOLES', 3: 'JUEVES', 4: 'VIERNES', 5: 'SABADO', 6: 'DOMINGO'
    }

    if request.method == 'POST':
        # 1. Obtener datos manuales del formulario
        aula_id_post = request.POST.get('aula_id')
        fecha_str = request.POST.get('fecha')
        hora_inicio_str = request.POST.get('hora_inicio')
        hora_fin_str = request.POST.get('hora_fin')
        
        # 2. Validación de campos obligatorios
        if not all([aula_id_post, fecha_str, hora_inicio_str, hora_fin_str]):
            messages.error(request, "Datos incompletos. Se requiere Aula, Fecha, Hora Inicio y Hora Fin.")
            return redirect(request.path + f'?aula_id={aula_id_post}' if aula_id_post else request.path)

        try:
            # 3. Conversión de datos
            fecha_reserva = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            hora_inicio = datetime.strptime(hora_inicio_str, '%H:%M').time()
            hora_fin = datetime.strptime(hora_fin_str, '%H:%M').time()
            
            # Validar que la hora de inicio sea menor que la hora de fin
            if hora_inicio >= hora_fin:
                messages.error(request, "La hora de inicio debe ser anterior a la hora de fin.")
                return redirect(request.path + f'?aula_id={aula_id_post}')

            aula = Aula.objects.get(id=aula_id_post)
            
            if fecha_reserva.weekday() >= 5: # 5 = Sábado, 6 = Domingo, REGLA 1 NO SABADO NI DOMINGO
                messages.error(request, "Error: No se pueden realizar reservas en fines de semana (Sábado o Domingo).")
                return redirect(request.path + f'?aula_id={aula_id_post}')
            
            dia_semana_clave = DIAS_MAP_WEEKDAY[fecha_reserva.weekday()]

            # Regla 2: No Fechas Pasadas
            if fecha_reserva < date.today():
                messages.error(request, "Error: No se pueden realizar reservas en fechas pasadas.")
                return redirect(request.path + f'?aula_id={aula_id_post}')

            # Regla 3: Máximo 2 reservas por semana
            # Calcular el Lunes de la semana de la reserva
            inicio_semana_reserva = fecha_reserva - timedelta(days=fecha_reserva.weekday())
            # Calcular el Domingo de esa semana
            fin_semana_reserva = inicio_semana_reserva + timedelta(days=6)
            
            conteo_reservas_semana = Reserva.objects.filter(
                profesor=profesor_obj,
                fecha_reserva__gte=inicio_semana_reserva,
                fecha_reserva__lte=fin_semana_reserva
            ).count()
            
            if conteo_reservas_semana >= 2:
                messages.error(request, f"Error: Ya ha alcanzado el límite de {conteo_reservas_semana} reservas para la semana del {inicio_semana_reserva.strftime('%d/%m')}.")
                return redirect(request.path + f'?aula_id={aula_id_post}')

            # 4. Validación de Superposiciones
            
            # 4a. Ocupaciones Fijas del Aula (Recurrente)
            # Necesitamos obtener los bloques recurrentes para la validación
            bloques_ocupados_val = obtener_bloques_recurrentes_ocupados(request, aula_id_post)
            dia_semana_clave = list(DIAS_MAP.keys())[fecha_reserva.weekday()]
            
            for bloque in bloques_ocupados_val['ocupaciones_aula']:
                if bloque['dia'] == dia_semana_clave:
                    # (InicioBloque < FinReserva) Y (FinBloque > InicioReserva)
                    if (bloque['horaInicio'] < hora_fin and bloque['horaFin'] > hora_inicio):
                        messages.error(request, f"Conflicto: El aula {aula_id_post} tiene una clase fija recurrente en ese horario.")
                        return redirect(request.path + f'?aula_id={aula_id_post}')

            # 4b. Ocupaciones Fijas del Profesor (Recurrente)
            for bloque in bloques_ocupados_val['ocupaciones_profesor']:
                 if bloque['dia'] == dia_semana_clave:
                    if (bloque['horaInicio'] < hora_fin and bloque['horaFin'] > hora_inicio):
                        messages.error(request, f"Conflicto: Usted tiene una clase fija recurrente en ese horario (en aula {bloque.get('aula__id', 'otra')}).")
                        return redirect(request.path + f'?aula_id={aula_id_post}')

            # 4c. Reservas Existentes del Aula (Puntual)
            reservas_aula_superpuestas = Reserva.objects.filter(
                aula=aula,
                fecha_reserva=fecha_reserva,
                hora_inicio__lt=hora_fin, # Inicio existente < Fin nueva
                hora_fin__gt=hora_inicio  # Fin existente > Inicio nueva
            ).exists()
            
            if reservas_aula_superpuestas:
                messages.error(request, f"Conflicto: Ya existe otra reserva puntual para el aula {aula_id_post} en ese periodo.")
                return redirect(request.path + f'?aula_id={aula_id_post}')

            # 4d. Reservas Existentes del Profesor (Puntual)
            reservas_profesor_superpuestas = Reserva.objects.filter(
                profesor=profesor_obj,
                fecha_reserva=fecha_reserva,
                hora_inicio__lt=hora_fin, 
                hora_fin__gt=hora_inicio 
            ).exists()

            if reservas_profesor_superpuestas:
                  messages.error(request, f"Conflicto: Usted ya tiene otra reserva puntual en ese periodo en otra aula.")
                  return redirect(request.path + f'?aula_id={aula_id_post}')

            # 5. Guardar la Reserva
            Reserva.objects.create(
                aula=aula,
                profesor=profesor_obj,
                fecha_reserva=fecha_reserva,
                hora_inicio=hora_inicio,
                hora_fin=hora_fin
            )
            messages.success(request, f"Reserva del aula {aula_id_post} para el {fecha_str} de {hora_inicio_str} a {hora_fin_str} creada con éxito.")
            return redirect(request.path + f'?aula_id={aula_id_post}')

        except Aula.DoesNotExist:
             messages.error(request, "El ID del Aula ingresado no existe.")
             return redirect(request.path + f'?aula_id={aula_id_post}')
        except Exception as e:
             messages.error(request, f"Error al procesar la reserva: {e}")
             return redirect(request.path + f'?aula_id={aula_id_post}')

    # --- FIN LÓGICA POST ---


    puntos_corte = set()
    puntos_corte.add(time(20,10)) #HORA FIJA FIN
    puntos_corte.add(time(7,0))
    for bloque in ocupaciones_aulas:
        puntos_corte.add(bloque['horaInicio'])
        puntos_corte.add(bloque['horaFin'])
    for bloque in ocupaciones_profesor:
        puntos_corte.add(bloque['horaInicio'])
        puntos_corte.add(bloque['horaFin'])
    for bloque in reservas_existentes:
        puntos_corte.add(bloque.hora_inicio.replace(second=0, microsecond=0))
        puntos_corte.add(bloque.hora_fin.replace(second=0, microsecond=0))

    dummy_date = date.today()
    puntos_corte_dt = sorted(list(set([datetime.combine(dummy_date, t) for t in puntos_corte])))

    #Mapeo de recurrencias pa acceso rápido
    mapa_rec_aula = {d: [] for d in DIAS_MAP.keys()}
    for bloque in ocupaciones_aulas:
        mapa_rec_aula[bloque['dia']].append(bloque)
        
    mapa_rec_profesor = {d: [] for d in DIAS_MAP.keys()}
    for bloque in ocupaciones_profesor:
        mapa_rec_profesor[bloque['dia']].append(bloque)

    horario_consolidado = []
    hora_actual = None

    for dt_siguiente in puntos_corte_dt:
        if hora_actual is None:
            hora_actual = dt_siguiente
            continue
        dt_inicio_fila = hora_actual
        dt_fin_fila = dt_siguiente
        if dt_inicio_fila >= dt_fin_fila:
            hora_actual = dt_siguiente
            continue
        
        hora_inicio_time = dt_inicio_fila.time()
        hora_fin_time = dt_fin_fila.time()
        rango_hora_str = f"{hora_inicio_time.strftime('%H:%M')} - {hora_fin_time.strftime('%H:%M')}"
        fila_data = []
        hay_clase_en_fila = False

        for fecha_especifica in dias_a_mostrar:
            dia_semana_str = list(DIAS_MAP.keys())[fecha_especifica.weekday()]
            estado_celda = {
                'tipo': 'LIBRE', 
                'color': COLOR_DISPONIBLE, 
                'texto': 'Disponible para Reservar',
                'fecha': fecha_especifica.strftime('%Y-%m-%d'),
                'horaInicio': dt_inicio_fila.strftime('%H:%M'), 
                'horaFin': dt_fin_fila.strftime('%H:%M'),
                'data_reserva': f"{fecha_especifica.strftime('%Y-%m-%d')}|{dt_inicio_fila.strftime('%H:%M')}|{dt_fin_fila.strftime('%H:%M')}",
            }

            for reserva in reservas_existentes:
                if reserva.fecha_reserva==fecha_especifica:
                    r_inicio_time = reserva.hora_inicio.replace(second=0, microsecond=0)
                    r_fin_time = reserva.hora_fin.replace(second=0, microsecond=0)

                    if (r_inicio_time <= hora_inicio_time and r_fin_time > hora_inicio_time) or \
                       (r_inicio_time < hora_fin_time and r_fin_time >= hora_fin_time) or \
                       (r_inicio_time >= hora_inicio_time and r_fin_time <= hora_fin_time):
                        
                        hay_clase_en_fila = True
                        if reserva.profesor.perfil.id == profesor_obj.perfil.id:
                            estado_celda.update({
                                'tipo': 'MI RESERVA',
                                'color': COLOR_MI_RESERVA,
                                'texto': "Mi Reserva",
                                'data_reserva': None,
                            })
                        else:
                            estado_celda.update({
                                'tipo': 'AULA_RESERVA', 
                                'color': COLOR_AULA_OCUPADA, 
                                'texto': f"Reservado ({reserva.profesor.perfil.nombre})",
                                'data_reserva': None,
                            })
                        break
                
            if estado_celda['tipo'] == 'LIBRE': 
                bloques_aula_rec = mapa_rec_aula.get(dia_semana_str, [])
                for bloque in bloques_aula_rec:
                    if (bloque['horaInicio'] <= hora_inicio_time and bloque['horaFin'] > hora_inicio_time) or \
                       (bloque['horaInicio'] < hora_fin_time and bloque['horaFin'] >= hora_fin_time) or \
                       (bloque['horaInicio'] >= hora_inicio_time and bloque['horaFin'] <= hora_fin_time):
                    
                        estado_celda.update({
                            'tipo': 'AULA_FIJA', 
                            'color': COLOR_AULA_OCUPADA, 
                            'texto': "Aula Ocupada (Clase Fija)",
                            'data_reserva': None,
                        })
                        break
                
            if estado_celda['tipo'] == 'LIBRE':
                bloques_prof_rec = mapa_rec_profesor.get(dia_semana_str,[])
                for bloque in bloques_prof_rec:
                    if (bloque['horaInicio'] <= hora_inicio_time and bloque['horaFin'] > hora_inicio_time) or \
                       (bloque['horaInicio'] < hora_fin_time and bloque['horaFin'] >= hora_fin_time) or \
                       (bloque['horaInicio'] >= hora_inicio_time and bloque['horaFin'] <= hora_fin_time):

                        estado_celda.update({
                         'tipo': 'PROFESOR_FIJO', 
                         'color': COLOR_PROFESOR_OCUPADO, 
                         'texto': f"Profesor Ocupado (Otra Clase en {bloque.get('aula__id', 'otra aula')})",
                         'data_reserva': None,
                        })
                        break
                
            if estado_celda['tipo'] != 'LIBRE':
                hay_clase_en_fila=True
            fila_data.append(estado_celda)
        
        if not hay_clase_en_fila and horario_consolidado and horario_consolidado[-1]['tipo']=='LIBRE':
            fila_anterior = horario_consolidado[-1]
            fila_anterior['rango'] = f"{fila_anterior['rango'].split(' - ')[0]} - {hora_fin_time.strftime('%H:%M')}"
            for celda in fila_anterior['data']:
                if celda['data_reserva']: # Si es reservable (LIBRE/PROFESOR_FIJO)
                    # El data_reserva es FECHA|HORA_INICIO|HORA_FIN. Actualizamos el HORA_FIN.
                    parts = celda['data_reserva'].split('|')
                    celda['data_reserva'] = f"{parts[0]}|{parts[1]}|{dt_fin_fila.strftime('%H:%M')}"
        else:
            horario_consolidado.append({
                'rango': rango_hora_str,
                'data': fila_data, # Contiene el estado de los 10 días
                'tipo': 'CLASE' if hay_clase_en_fila else 'LIBRE',
            })
        hora_actual = dt_siguiente
    dias_para_encabezado = [f"{DIAS_MAP[list(DIAS_MAP.keys())[d.weekday()]]} {d.strftime('%d/%m')}" for d in dias_a_mostrar]

    mis_reservas_recientes = Reserva.objects.filter(
        profesor=profesor_obj,
        fecha_reserva__gte=date.today()
    ).order_by('fecha_reserva', 'hora_inicio').select_related('aula')

    return render(request, 'usuarios/profesor/reservar_aula.html',{
        'dias_a_mostrar': dias_para_encabezado,
        'horario_consolidado': horario_consolidado,
        'aula_actual_id': aula_id_a_filtrar,
        'aulas_existentes': aulas_existentes,
        'mis_reservas_recientes': mis_reservas_recientes,
    })

def cancelar_reserva(request):
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    if request.method != 'POST':
        messages.error(request, "Método no permitido.")
        return redirect('usuarios:reservar_aula') # Redirige a la pág principal de reservas

    try:
        reserva_id = request.POST.get('reserva_id')
        profesor_obj, _ = check_professor_auth(request) # Verifica que el profesor esté logueado

        if not reserva_id or not profesor_obj:
            messages.error(request, "Datos incompletos para la cancelación.")
            return redirect(request.META.get('HTTP_REFERER', 'usuarios:reservar_aula'))

        # 4. Validación de Seguridad CRÍTICA:
        # Asegurarse de que la reserva existe Y le pertenece al profesor logueado.
        reserva_a_borrar = Reserva.objects.get(
            id=reserva_id,
            profesor=profesor_obj 
        )
        
        # 5. Borrado
        reserva_a_borrar.delete()
        messages.success(request, "Reserva cancelada exitosamente.")

    except Reserva.DoesNotExist:
        messages.error(request, "No se pudo encontrar la reserva o no tienes permiso para cancelarla.")
    except Exception as e:
        messages.error(request, f"Ocurrió un error: {e}")

    # Redirigir a la página anterior (que usualmente es la de reservar_aula)
    return redirect(request.META.get('HTTP_REFERER', 'usuarios:reservar_aula'))


def reservar_aula(request):
    profesor_obj, response = check_professor_auth(request)
    if response: return response
    contexto = {'perfil': profesor_obj.perfil, 'titulo': 'Reserva de Aulas'}
    return render(request, 'usuarios/profesor/reservar_aula.html', contexto)

CAMPOS_NOTA = ['EP1', 'EC1', 'EP2', 'EC2', 'EP3', 'EC3'] 

def subida_notas(request):
    """
    Vista principal para la carga de notas y visualización de estadísticas, 
    ocultando la carga a profesores de laboratorios.
    """
    # 1. Autenticación y Contexto Base
    profesor_obj, response = check_professor_auth(request)
    if response: return response

    grupos_asignados = GrupoCurso.objects.filter(profesor=profesor_obj).select_related('curso').order_by('curso__id', 'grupo')
    
    grupo_id_url = request.GET.get('grupo', None)
    grupo_seleccionado = None
    estudiantes_matriculados = []
    estadisticas = {}
    
    # NUEVA VARIABLE DE CONTROL
    es_grupo_laboratorio = False 
    
    # 2. Manejo de la subida de notas (POST)
    # Se recomienda que el POST también verifique si es un grupo de laboratorio 
    # antes de intentar guardar, aunque el template lo oculte.
    if request.method == 'POST':
        grupo_id_post = request.POST.get('grupo_id')
        if not grupo_id_post:
            messages.error(request, "Faltan datos del grupo para la actualización.")
            return redirect(request.path)
            
        try:
            # Obtener el grupo e incluir la relación para verificar si es laboratorio
            grupo = GrupoCurso.objects.select_related('grupolaboratorio').get(id=grupo_id_post, profesor=profesor_obj)
            
            # **VERIFICACIÓN DE SEGURIDAD CLAVE**
            if hasattr(grupo, 'grupolaboratorio') and grupo.grupolaboratorio is not None:
                messages.error(request, "Acción denegada. No se permite la carga manual de notas para grupos de Laboratorio.")
                return redirect(f"{request.path}?grupo={grupo_id_post}")
            # **FIN VERIFICACIÓN DE SEGURIDAD**

            with transaction.atomic():
                updated_count = 0
                for key, value in request.POST.items():
                    if key.startswith('nota_'):
                        parts = key.split('_')
                        if len(parts) == 3:
                            estudiante_id = parts[1]
                            campo_nota = parts[2]
                            
                            if campo_nota in CAMPOS_NOTA:
                                try:
                                    matricula = Matricula.objects.get(grupo_curso=grupo, estudiante__perfil__id=estudiante_id)
                                    
                                    value_strip = value.strip()
                                    nota_float = None 

                                    if value_strip != '':
                                        nota_float = float(value_strip)
                                        if not (0 <= nota_float <= 20):
                                            messages.error(request, f"Nota fuera de rango (0-20) para CUI {estudiante_id} en {campo_nota}.")
                                            continue 
                                            
                                    current_value = getattr(matricula, campo_nota)
                                    should_update = False
                                    
                                    # Lógica de comparación de valores
                                    if current_value != nota_float:
                                         if current_value is None and nota_float is not None:
                                            should_update = True
                                         elif current_value is not None and nota_float is None:
                                            should_update = True
                                         elif current_value is not None and nota_float is not None:
                                            if abs(current_value - nota_float) > 0.01:
                                                should_update = True

                                    if should_update:
                                        setattr(matricula, campo_nota, nota_float)
                                        matricula.save()
                                        updated_count += 1
                                            
                                except ObjectDoesNotExist:
                                    messages.error(request, f"Matrícula no encontrada para CUI {estudiante_id} en este grupo.")
                                    pass 
                                except ValueError:
                                    messages.error(request, f"Formato de nota inválido para CUI {estudiante_id} en {campo_nota}.")
                                    pass 

                if updated_count > 0:
                    messages.success(request, f"¡{updated_count} notas actualizadas con éxito para el grupo {grupo.curso.id}!")
                else:
                    messages.info(request, "No se detectaron cambios en las notas enviadas.")

            return redirect(f"{request.path}?grupo={grupo_id_post}")

        except ObjectDoesNotExist:
            messages.error(request, "El grupo de curso seleccionado no existe o no está asignado a usted.")
            return redirect(request.path)
        except Exception as e:
            messages.error(request, f"Ocurrió un error general: {e}")
            return redirect(f"{request.path}") 


    # 3. Manejo de la visualización de datos (GET) y Estadísticas
    if grupo_id_url:
        try:
            # Incluir select_related para verificar si es un grupo de Laboratorio
            grupo_seleccionado = GrupoCurso.objects.select_related(
                'curso',
                'grupolaboratorio' # Asume que esta es la relación OneToOneField inversa
            ).get(id=grupo_id_url, profesor=profesor_obj)
            
            # **LÓGICA CLAVE: Determinar si es un grupo de Laboratorio**
            # Si el OneToOneField a GrupoLaboratorio existe (no es None), es de Lab.
            if hasattr(grupo_seleccionado, 'grupolaboratorio') and grupo_seleccionado.grupolaboratorio is not None:
                 es_grupo_laboratorio = True
            
            # Obtener estudiantes y calcular estadísticas, independientemente de si es Lab o no.
            estudiantes_matriculados = Matricula.objects.filter(
                grupo_curso=grupo_seleccionado
            ).select_related('estudiante__perfil').order_by('estudiante__perfil__nombre')

            # 3.1. Cálculo de Estadísticas (Max, Min, Avg)
            aggregate_fields = {}
            for campo in CAMPOS_NOTA:
                aggregate_fields[f'{campo}_max'] = Coalesce(Max(F(campo)), Value(0.0))
                aggregate_fields[f'{campo}_min'] = Coalesce(Min(F(campo)), Value(0.0))
                aggregate_fields[f'{campo}_avg'] = Coalesce(Avg(F(campo)), Value(0.0))
            
            estadisticas_raw = estudiantes_matriculados.aggregate(**aggregate_fields)
            
            # 3.2. Formatear Estadísticas para el Template
            for campo in CAMPOS_NOTA:
                avg_key = f'{campo}_avg'
                max_key = f'{campo}_max'
                min_key = f'{campo}_min'
                
                estadisticas[campo] = {
                    'avg': f"{estadisticas_raw[avg_key]:.2f}",
                    'max': f"{estadisticas_raw[max_key]:.1f}",
                    'min': f"{estadisticas_raw[min_key]:.1f}",
                }
            
        except ObjectDoesNotExist:
            messages.warning(request, f"El grupo {grupo_id_url} no fue encontrado o no está asignado a usted.")
            grupo_seleccionado = None
            grupo_id_url = None

    # 4. Contexto final para el template
    contexto = {
        'perfil': profesor_obj.perfil, 
        'titulo': 'Carga de Calificaciones',
        'grupos_asignados': grupos_asignados,
        'grupo_seleccionado': grupo_seleccionado,
        'grupo_id_url': grupo_id_url,
        'estudiantes_matriculados': estudiantes_matriculados,
        'campos_nota': CAMPOS_NOTA, 
        'estadisticas': estadisticas, 
        'es_grupo_laboratorio': es_grupo_laboratorio, # <--- AÑADIDO AL CONTEXTO
    }
    return render(request, 'usuarios/profesor/subida_notas.html', contexto)

# ----------------------------------------------------------------------
# 2. VISTAS DE LA SECRETARIA
# ----------------------------------------------------------------------
def check_secretaria_auth(request):
    """Función de ayuda para verificar la sesión y el rol de Secretaria."""
    if not request.session.get('is_authenticated') or request.session.get('usuario_rol') != 'SECRETARIA':
        messages.warning(request, "Acceso denegado o rol incorrecto.")
        return None, redirect('usuarios:selector_rol')
    
    usuario_id = request.session['usuario_id']
    try:
        # Aquí solo necesitamos el Perfil, ya que la secretaria no tiene un modelo propio
        perfil_obj = Perfil.objects.get(id=usuario_id)
        return perfil_obj, None
    except Perfil.DoesNotExist:
        messages.error(request, "Error: Datos de perfil no encontrados. Cierre de sesión forzado.")
        return None, redirect('usuarios:logout')

def dashboard_secretaria(request):
    """Muestra la página de inicio del dashboard de Secretaría."""
    perfil_obj, response = check_secretaria_auth(request)
    if response:
        return response
    
    # Datos de ejemplo para el dashboard
    conteo_profesores = Profesor.objects.count()
    conteo_estudiantes = Estudiante.objects.count()
    
    contexto = {
        'perfil': perfil_obj,
        'titulo': 'Inicio - Panel Secretaría',
        'conteo_profesores': conteo_profesores,
        'conteo_estudiantes': conteo_estudiantes,
    }
    return render(request, 'usuarios/secretaria/dashboard_secretaria.html', contexto)

def mi_cuenta_secretaria(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: return response
    contexto = {'perfil': perfil_obj, 'titulo': 'Mi Cuenta'}
    return render(request, 'usuarios/secretaria/mi_cuenta_secretaria.html', contexto)

def gestion_cursos(request):
    """Muestra todos los cursos con el conteo de grupos de Teoría y Laboratorio."""
    perfil_obj, response = check_secretaria_auth(request)
    if response: return response

    # Obtiene todos los cursos, anotando la cantidad de grupos de teoría y lab.
    # Usamos Count y el related_name inverso para contar los grupos de teoría y lab.
    # total_grupos cuenta la cantidad total de GrupoCurso asociados al Curso.
    cursos = Curso.objects.annotate(
        total_grupos=Count('grupocurso'),
        grupos_teoria_count=Count('grupocurso__grupoteoria', distinct=True),
        grupos_laboratorio_count=Count('grupocurso__grupolaboratorio', distinct=True),
    ).order_by('id')
    
    contexto = {
        'perfil': perfil_obj, 
        'titulo': 'Gestión de Cursos',
        'cursos': cursos # Pasamos el queryset anotado a la plantilla
    }
    return render(request, 'usuarios/secretaria/gestion_cursos.html', contexto)

def ver_horarios_clases(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: return response
    contexto = {'perfil': perfil_obj, 'titulo': 'Visualización de Horarios'}
    return render(request, 'usuarios/secretaria/ver_horarios_clases.html', contexto)

def gestion_laboratorios(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: return response
    contexto = {'perfil': perfil_obj, 'titulo': 'Gestión de Laboratorios'}
    return render(request, 'usuarios/secretaria/gestion_laboratorios.html', contexto)

def registro_estudiantes(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: return response

    # Recupera todos los estudiantes (datos dinámicos)
    estudiantes = Estudiante.objects.select_related('perfil').all().order_by('perfil__nombre')
    
    contexto = {
        'perfil': perfil_obj, 
        'titulo': 'Registro de Estudiantes',
        'estudiantes': estudiantes
    }
    return render(request, 'usuarios/secretaria/registro_estudiantes.html', contexto)

def registro_profesores(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: return response
    
    # Recupera todos los profesores (datos dinámicos)
    profesores = Profesor.objects.select_related('perfil').all().order_by('perfil__nombre')

    contexto = {
        'perfil': perfil_obj, 
        'titulo': 'Registro de Profesores',
        'profesores': profesores
    }
    return render(request, 'usuarios/secretaria/registro_profesores.html', contexto)