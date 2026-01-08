# usuarios/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.urls import reverse
from django.db.models import Count, Prefetch, Q, F, Case, When, Sum, ExpressionWrapper, DecimalField, FloatField, Value, Avg, Max, Min
from django.db import transaction
from django.db.models.functions import Coalesce
from django.db.utils import IntegrityError
from django.core.exceptions import ObjectDoesNotExist
from .models import Perfil, Estudiante, Profesor, Secretaria, Administrador
from .forms import CursoForm, GrupoCursoForm, BloqueHorarioForm
from cursos.models import Curso, BloqueHorario, GrupoTeoria, GrupoLaboratorio, GrupoCurso, TemaCurso
from matriculas.models import Matricula, MatriculaLaboratorio
from reservas.models import Aula, Reserva
from asistencias.models import RegistroAsistencia, RegistroAsistenciaDetalle
from django.utils import timezone
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.template.loader import render_to_string
from datetime import datetime, time, timedelta, date
from xhtml2pdf import pisa

import datetime as dt
import math
import io
import openpyxl
import csv
import json

# Librerías que usamos para exportar
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


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
            # 1. Buscar Perfil por email y rol
            usuario_perfil = Perfil.objects.get(
                email=email_ingresado,
                rol=rol_upper
            )

            # 1.1 Verificar si la cuenta está inactiva
            if usuario_perfil.estadoCuenta is False:
                messages.error(request, "Esta cuenta está inhabilitada. Contacte a secretaría.")
                return render(request, 'usuarios/login.html', {'rol': rol_upper})

            # 2. Verificar contraseña
            if usuario_perfil.password != password_ingresada:
                messages.error(request, "Contraseña incorrecta.")
                return render(request, 'usuarios/login.html', {'rol': rol_upper})

            # 3. Si pasa las verificaciones: iniciar sesión manual
            request.session['usuario_id'] = usuario_perfil.id
            request.session['usuario_rol'] = usuario_perfil.rol
            request.session['is_authenticated'] = True

            # 4. Redirigir por rol
            if usuario_perfil.rol == 'ESTUDIANTE':
                return redirect('usuarios:dashboard_estudiante')
            if usuario_perfil.rol == 'PROFESOR':
                return redirect('usuarios:dashboard_profesor')
            if usuario_perfil.rol == 'SECRETARIA':
                return redirect('usuarios:dashboard_secretaria')
            if usuario_perfil.rol == 'ADMIN':
                return redirect('usuarios:dashboard_admin')

        except Perfil.DoesNotExist:
            messages.error(request, f"Credenciales inválidas para el rol de {rol_upper}.")
    
    return render(request, 'usuarios/login.html', {'rol': rol_upper})

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
        messages.error(request, "Error: Datos de estudiante no encontrados. Cierre de sesión forzado.")
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
    # 1. Autenticación y Obtención del Perfil
    # Esta función debe devolver (estudiante_obj, None) si el usuario es válido,
    # o (None, response) si debe ser redirigido.
    estudiante_obj, response = check_student_auth(request)
    if response: return response
    
    perfil = estudiante_obj.perfil # Obtenemos el objeto Perfil del estudiante
    
    # ----------------------------------------------------
    # POST Request: Lógica para Cambiar Contraseña
    # ----------------------------------------------------
    if request.method == 'POST':
        # Captura de datos del formulario (los names del HTML)
        old_password = request.POST.get('old_password')
        new_password1 = request.POST.get('new_password1')
        new_password2 = request.POST.get('new_password2')
        
        # --- 1. Verificación de Contraseña Actual ---
        if old_password != perfil.password:
            messages.error(request, 'La contraseña actual ingresada es incorrecta.')
            return redirect('usuarios:mi_cuenta_alumno') 

        # --- 2. Verificación de Coincidencia y Longitud ---
        if new_password1 != new_password2:
            messages.error(request, 'La nueva contraseña y su confirmación no coinciden.')
            return redirect('usuarios:mi_cuenta_alumno')
            
        if len(new_password1) < 6:
            messages.error(request, 'La nueva contraseña debe tener al menos 6 caracteres.')
            return redirect('usuarios:mi_cuenta_alumno')
            
        if new_password1 == old_password:
            messages.warning(request, 'La nueva contraseña no puede ser igual a la anterior.')
            return redirect('usuarios:mi_cuenta_alumno')


        # --- 3. Actualización y Guardado ---
        try:
            # Sobrescribe la contraseña en el modelo Perfil
            perfil.password = new_password1 
            perfil.save()
            
            messages.success(request, '¡Contraseña actualizada con éxito! Debe usar la nueva contraseña en el próximo inicio de sesión.')
            
        except Exception as e:
            messages.error(request, f'Error del sistema al guardar la nueva contraseña: {e}')

        # Redirige para mostrar el mensaje y limpiar el formulario
        return redirect('usuarios:mi_cuenta_alumno')

    # ----------------------------------------------------
    # GET Request: Mostrar Formulario
    # ----------------------------------------------------
    contexto = {
        'perfil': perfil,
        'titulo': 'Mi Cuenta',
    }
    # Asegúrate que 'usuarios/alumno/mi_cuenta.html' sea la ruta correcta del template.
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
    # 2. Consulta B: Matrículas de Teoría y Estructuración de Datos (OPTIMIZADA Y CORREGIDA)
    # ----------------------------------------------------------------------
    
    # Obtenemos las matrículas de teoría del estudiante (asumiendo estado=True para activos)
    # CORRECCIÓN: La relación es GrupoCurso -> GrupoTeoria (accesor: grupoteoria) -> TemaCurso (accesor: temacurso_set)
    matriculas_teoria_activas = Matricula.objects.filter(
        estudiante=estudiante_obj, 
        estado=True
    ).select_related(
        'grupo_curso__curso', 
        'grupo_curso__profesor__perfil'
    ).prefetch_related(
        'grupo_curso__grupoteoria__temacurso_set'
    ).order_by(
        'grupo_curso__curso__nombre'
    )

    cursos_info = []
    
    for mat_teoria in matriculas_teoria_activas:
        grupo_teoria = mat_teoria.grupo_curso # Este es un objeto GrupoCurso
        curso = grupo_teoria.curso
        
        # Verificar si el curso tiene sílabo subido
        tiene_silabo = bool(curso.silabo_url)

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
        
        # Acceso CORREGIDO a los temas
        temas_del_curso = []
        try:
            # grupo_teoria (GrupoCurso) tiene el accesor 'grupoteoria' (GrupoTeoria)
            # y GrupoTeoria tiene el accesor inverso 'temacurso_set' (Temas)
            temas_del_curso = list(grupo_teoria.grupoteoria.temacurso_set.all())
        except GrupoTeoria.DoesNotExist:
            # Esto maneja si el GrupoCurso no está especializado como GrupoTeoria 
            # (aunque debería estarlo si tiene una Matricula)
            pass
        
        cursos_info.append({
            'curso_id': curso.id,
            'nombre_curso': curso.nombre,
            'grupo_teoria': grupo_teoria.grupo,
            'profesor_teoria': profesor_teoria_nombre,
            'grupo_lab': grupo_lab_info['grupo'],
            'profesor_lab': grupo_lab_info['profesor'],
            'grupo_lab_id': grupo_lab_info['id'], 
            'temas': temas_del_curso, 
            'collapse_id': f'temas-{curso.id}',
            'tiene_silabo': tiene_silabo,
            'silabo_url': curso.silabo_url if tiene_silabo else None,
            'collapse_silabo_id': f'silabo-{curso.id}'  # <-- NUEVO: ID para el collapse del sílabo
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

        duracion_minutos = (dt_fin_fila - dt_inicio_fila).total_seconds() / 60
        if duracion_minutos < 11:
            hora_actual = dt_siguiente
            continue
        
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
    estudiante_obj, response = check_student_auth(request)
    if response: return response

    TARGET_PASSING_SCORE = 10.5

    # 1. Obtener todas las matrículas del estudiante con información relacionada
    matriculas = Matricula.objects.filter(
        estudiante=estudiante_obj
    ).select_related(
        'grupo_curso__curso', 
        'grupo_curso__profesor__perfil'
    )
    
    # Lista de cursos matriculados para el selector (Dropdown)
    cursos_matriculados = []
    
    # Lista para el GRÁFICO GENERAL (Se llena al final del loop)
    promedios_generales = [] 

    # Inicializar datos para el detalle de notas
    curso_seleccionado_data = None
    curso_id_seleccionado = request.GET.get('curso')
    
    # --- PROCESAMIENTO GENERAL PARA GENERAR PROMEDIOS ---
    for m in matriculas:
        
        # Datos básicos para el selector
        cursos_matriculados.append({
            'curso_id': m.grupo_curso.curso.id,
            'nombre_curso': m.grupo_curso.curso.nombre,
        })

        # Pre-cálculo para el gráfico general
        grupo_curso = m.grupo_curso
        curso_obj = grupo_curso.curso
        
        evaluaciones = [
            {'nombre': 'Examen Parcial 1 (EP1)', 'nota_field': 'EP1', 'porcentaje_field': 'porcentajeEP1'},
            {'nombre': 'Nota Continua 1 (EC1)', 'nota_field': 'EC1', 'porcentaje_field': 'porcentajeEC1'},
            {'nombre': 'Examen Parcial 2 (EP2)', 'nota_field': 'EP2', 'porcentaje_field': 'porcentajeEP2'},
            {'nombre': 'Nota Continua 2 (EC2)', 'nota_field': 'EC2', 'porcentaje_field': 'porcentajeEC2'},
            {'nombre': 'Examen Parcial 3 (EP3)', 'nota_field': 'EP3', 'porcentaje_field': 'porcentajeEP3'},
            {'nombre': 'Nota Continua 3 (EC3)', 'nota_field': 'EC3', 'porcentaje_field': 'porcentajeEC3'},
        ]
        
        promedio_final_temp = 0.0
        total_porcentaje_temp = 0 # Ponderación Cubierta
        
        for eval_data in evaluaciones:
            nota = getattr(m, eval_data['nota_field'])
            porcentaje = getattr(curso_obj, eval_data['porcentaje_field'])

            if porcentaje is not None and porcentaje > 0:
                if nota is not None:
                    promedio_final_temp += (nota * porcentaje) / 100
                
                # total_weight_missing_temp se calcula implícitamente fuera del loop como 100 - total_porcentaje_temp
                total_porcentaje_temp += porcentaje 
        
        # El promedio_final_estimado incluye solo las notas puestas (sin las faltantes)
        promedios_generales.append({
            'id': curso_obj.id,
            'nombre': curso_obj.nombre,
            'promedio_final': round(promedio_final_temp, 1) if promedio_final_temp is not None else 0.0,
            
            # DATOS GRÁFICO DONA GENERAL
            'porcentaje_cubierto': total_porcentaje_temp,
            'porcentaje_faltante': 100 - total_porcentaje_temp
        })
    # --- FIN DE PROCESAMIENTO GENERAL ---
    
    

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
            total_porcentaje = 0 # Ponderación Cubierta
            
            # === VARIABLES DE CÁLCULO DE META ===
            MAX_GRADE = 20.0
            TARGET_WEIGHTED_SUM = TARGET_PASSING_SCORE * 100 # 1050
            
            current_weighted_score = 0.0 # Suma de (Nota * Porcentaje) de notas registradas
            total_weight_missing = 0.0 # Suma de Porcentaje de notas faltantes
            missing_evaluations_count = 0
            
            missing_notes_list = [] 
            
            # --- Variables para el GRÁFICO DE BARRAS INDIVIDUAL ---
            chart_bar_labels = [] 
            chart_bar_grades = []
            chart_bar_weights = []
            # =======================================================
            
            # --- ADICIÓN DE VARIABLES PARA GRÁFICO DE DISTRIBUCIÓN ---
            ponderacion_aprobada = 0.0
            ponderacion_reprobada = 0.0
            # ---------------------------------------------------------

            
            # 3. Procesar y calcular el promedio ponderado y métricas de notas faltantes
            for eval_data in evaluaciones:
                nota = getattr(matricula_seleccionada, eval_data['nota_field'])
                porcentaje = getattr(curso_obj, eval_data['porcentaje_field'])

                if porcentaje is not None and porcentaje > 0:
                    
                    # Datos para el detalle en tabla y para el gráfico
                    nota_display = nota if nota is not None else 'N/A'
                    notas_detalle.append({
                        'nombre': eval_data['nombre'],
                        'peso': porcentaje,
                        'nota': nota_display 
                    })
                    
                    # Llenar datos para el gráfico de barras individual
                    chart_bar_labels.append(eval_data['nombre'].split('(')[1].replace(')', '')) # Ej: EP1
                    chart_bar_weights.append(porcentaje)
                    
                    # Lógica de cálculo de promedios
                    if nota is not None:
                        current_weighted_score += (nota * porcentaje)
                        promedio_final += (nota * porcentaje) / 100
                        chart_bar_grades.append(nota)
                        
                        # --- LÓGICA DE CÁLCULO PARA GRÁFICO DE DISTRIBUCIÓN ---
                        if nota >= TARGET_PASSING_SCORE:
                            ponderacion_aprobada += porcentaje
                        else:
                            ponderacion_reprobada += porcentaje
                        # -----------------------------------------------------
                        
                    else:
                        total_weight_missing += porcentaje
                        missing_evaluations_count += 1
                        
                        missing_notes_list.append({
                            'name': eval_data['nombre'],
                            'weight': porcentaje,
                            'field': eval_data['nota_field']
                        })
                        chart_bar_grades.append(None)
                        
                    total_porcentaje += porcentaje

            # === 4. CÁLCULO DE LA NOTA MÍNIMA REQUERIDA Y OPCIONES ===
            required_avg_grade = None
            is_impossible = False
            approval_scenarios = [] 
            required_weighted_sum = 0.0 

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
                    
                    missing_notes_list.sort(key=lambda x: x['weight'], reverse=True)
                    
                    top_missing_note_1 = missing_notes_list[0]
                    top_missing_note_2 = missing_notes_list[1]
                    
                    P1 = top_missing_note_1['weight']
                    P2 = top_missing_note_2['weight']
                    
                    required_weighted_sum_for_P1_P2 = required_weighted_sum 

                    def calculate_scenario(N1_grade, P1, P2, required_sum, current_score_sum):
                        """Calcula N2, el promedio final y verifica la validez."""
                        N1_contribution = N1_grade * P1
                        required_by_N2 = required_sum - N1_contribution
                        
                        N2_grade = required_by_N2 / P2 if P2 > 0 else MAX_GRADE + 1

                        # Redondear N2 al entero más cercano hacia arriba (math.ceil)
                        N2_grade_rounded = max(0.0, math.ceil(min(MAX_GRADE, N2_grade)))
                        
                        # Cálculo basado en la suma ponderada actual + las 2 notas clave
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

                    # Eliminar duplicados y ordenar
                    unique_scenarios = []
                    seen = set()
                    for s in approval_scenarios:
                        key = (s['N1_grade'], s['N2_grade'], s['final_average'])
                        if key not in seen:
                            seen.add(key)
                            unique_scenarios.append(s)
                            
                    approval_scenarios = sorted(unique_scenarios, key=lambda x: x['N1_grade'])
                    
                    # Escenario Imposible
                    if not approval_scenarios and is_impossible:
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
                        
                # ... Fin de la lógica de escenarios ...

            # 5. Preparar el objeto de contexto para el detalle del curso
            profesor_nombre = profesor_obj.perfil.nombre if profesor_obj else 'No Asignado'
            
            curso_seleccionado_data = {
                'id': curso_obj.id,
                'nombre': curso_obj.nombre,
                'grupo': grupo_curso.grupo,
                'profesor': profesor_nombre,
                'notas': notas_detalle,
                'promedio_final': round(promedio_final, 1) if promedio_final is not None else None, 
                
                # DATOS GRÁFICO DONA INDIVIDUAL: Ponderación cubierta y faltante
                'total_porcentaje': total_porcentaje, 
                'total_weight_missing': total_weight_missing,
                
                # --- DATOS ADICIONALES PARA GRÁFICO DE DISTRIBUCIÓN ---
                'ponderacion_aprobada': round(ponderacion_aprobada, 1),
                'ponderacion_reprobada': round(ponderacion_reprobada, 1),
                'ponderacion_pendiente': round(total_weight_missing, 1),
                'nota_minima_aprobacion': TARGET_PASSING_SCORE,
                # -----------------------------------------------------

                # Variables de meta (Tu lógica de escenarios)
                'required_avg_grade': required_avg_grade,
                'missing_evaluations_count': missing_evaluations_count,
                'is_impossible': is_impossible,
                'required_weighted_sum': required_weighted_sum,
                'approval_scenarios': approval_scenarios,
                
                # --- DATOS PARA GRÁFICOS INDIVIDUALES ---
                'chart_bar_labels': chart_bar_labels,
                'chart_bar_grades': chart_bar_grades,
                'chart_bar_weights': chart_bar_weights
            }

        except Matricula.DoesNotExist:
            print(f"Error: Matrícula no encontrada para el curso {curso_id_seleccionado}")
            pass
        except Exception as e:
            print(f"Error procesando notas: {e}") 
            pass


    contexto = {
        'perfil': estudiante_obj.perfil,
        'titulo': 'Mis Notas',
        'cursos_matriculados': cursos_matriculados,
        'curso_seleccionado_data': curso_seleccionado_data,
        'curso_id_seleccionado': curso_id_seleccionado,
        'NOTA_MINIMA_APROBACION': TARGET_PASSING_SCORE,
        
        # --- DATOS PARA EL GRÁFICO GENERAL ---
        'promedios_generales': promedios_generales 
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
    
    hoy = date.today()

    # --- 1. Cursos Asignados (stats.cursos_asignados_count) ---
    # Contar todos los grupos donde el profesor_obj es el profesor asignado
    cursos_asignados_count = GrupoCurso.objects.filter(profesor=profesor_obj).count()

    days_until_friday = (4 - hoy.weekday() + 7) % 7 
    fecha_viernes = hoy + dt.timedelta(days=days_until_friday)

    # 2. Contar las reservas de aula hechas por este profesor desde hoy hasta el viernes más cercano
    # Se usa __range=[fecha_inicio, fecha_fin]
    reservas_activas_semana = Reserva.objects.filter(
        profesor=profesor_obj,
        fecha_reserva__range=[hoy, fecha_viernes] 
    ).count()

    # --- 3. Asistencia (stats.asistencia_resumen_hoy) ---
    
    # Mapear el día de la semana de Python (0=Lunes, 1=Martes...) a los choices de Django
    dia_semana_choices = {
        0: 'LUNES', 1: 'MARTES', 2: 'MIERCOLES',
        3: 'JUEVES', 4: 'VIERNES',
    }
    dia_actual_str = dia_semana_choices.get(hoy.weekday(), None)

    grupos_del_profesor = GrupoCurso.objects.filter(profesor=profesor_obj)
    clases_programadas_hoy_count = 0
    clases_con_registro_count = 0
    asistencia_resumen_hoy = "0/0"
    
    if dia_actual_str:
        # A) Clases programadas hoy: Bloques de horario que le corresponden al profesor hoy
        clases_programadas_hoy_count = BloqueHorario.objects.filter(
            grupo_curso__in=grupos_del_profesor,
            dia=dia_actual_str
        ).count()
        
        # B) Clases con registro: Registros de asistencia completados por el profesor hoy
        # Nota: La lógica ideal también podría chequear la hora para ver si la clase ya pasó.
        clases_con_registro_count = RegistroAsistencia.objects.filter(
            grupo_curso__in=grupos_del_profesor,
            fechaClase=hoy
        ).count()
        
        # Formatear el resumen (Ej: 3/4)
        if clases_programadas_hoy_count > 0 or clases_con_registro_count > 0:
             asistencia_resumen_hoy = f"{clases_con_registro_count}/{clases_programadas_hoy_count}"


    # --- Ensamblar el Contexto ---
    contexto = {
        'perfil': profesor_obj.perfil,
        'profesor': profesor_obj,
        'titulo': 'Inicio - Panel Docente',
        'stats': {
            'cursos_asignados_count': cursos_asignados_count,
            'reservas_activas_semana': reservas_activas_semana,
            'asistencia_resumen_hoy': asistencia_resumen_hoy, # Formato "X/Y"
        }
    }
    
    return render(request, 'usuarios/profesor/dashboard_profesor.html', contexto)

def mi_cuenta_profesor(request):
    # Autenticación
    profesor_obj, response = check_professor_auth(request)
    if response: 
        return response

    perfil = profesor_obj.perfil

    # -----------------------------
    # POST: cambio de contraseña
    # -----------------------------
    if request.method == 'POST':
        old_password = request.POST.get('old_password')
        new_password1 = request.POST.get('new_password1')
        new_password2 = request.POST.get('new_password2')

        # 1. Validar contraseña actual
        if old_password != perfil.password:
            messages.error(request, 'La contraseña actual ingresada es incorrecta.')
            return redirect('usuarios:mi_cuenta_profesor')

        # 2. Validar nueva contraseña
        if new_password1 != new_password2:
            messages.error(request, 'La nueva contraseña y su confirmación no coinciden.')
            return redirect('usuarios:mi_cuenta_profesor')

        if len(new_password1) < 6:
            messages.error(request, 'La nueva contraseña debe tener al menos 6 caracteres.')
            return redirect('usuarios:mi_cuenta_profesor')

        if new_password1 == old_password:
            messages.warning(request, 'La nueva contraseña no puede ser igual a la anterior.')
            return redirect('usuarios:mi_cuenta_profesor')

        # 3. Guardar
        try:
            perfil.password = new_password1
            perfil.save()
            messages.success(request, '¡Contraseña actualizada correctamente!')
        except Exception as e:
            messages.error(request, f'Error al guardar la nueva contraseña: {e}')

        return redirect('usuarios:mi_cuenta_profesor')

    # -----------------------------
    # GET: mostrar página
    # -----------------------------
    contexto = {
        'perfil': perfil,
        'titulo': 'Mi Cuenta'
    }
    return render(request, 'usuarios/profesor/mi_cuenta_profesor.html', contexto)

def mis_cursos_profesor(request):
    profesor_obj, response = check_professor_auth(request)
    if response:
        return response

    # ---------------------------------------------------------
    # 1. Lista de grupos
    # ---------------------------------------------------------
    teoria_groups = GrupoTeoria.objects.all()
    laboratorio_groups = GrupoLaboratorio.objects.all()

    carga_academica = GrupoCurso.objects.filter(
        profesor=profesor_obj
    ).select_related(
        'curso'
    ).prefetch_related(
        Prefetch('grupoteoria', queryset=teoria_groups, to_attr='teoria'),
        Prefetch('grupolaboratorio', queryset=laboratorio_groups, to_attr='laboratorio'),
    ).order_by('curso__id', 'grupo')

    cursos_procesados = []

    for g in carga_academica:
        # 1. Procesar g.teoria
        if g.teoria:
            try:
                # Intenta tomar el primer elemento (si es una lista/QuerySet)
                g.teoria = g.teoria[0]
            except TypeError:
                # Si falla con TypeError, es porque ya es el objeto simple. No hacemos nada.
                pass 
            except IndexError:
                # Si la lista está vacía, establece a None.
                g.teoria = None
        else:
            g.teoria = None


        # 2. Procesar g.laboratorio
        if g.laboratorio:
            try:
                g.laboratorio = g.laboratorio[0]
            except TypeError:
                pass
            except IndexError:
                g.laboratorio = None
        else:
            g.laboratorio = None

        if g.teoria:
            g.tipo = "Teoría"
            g.grupo_teoria = g.teoria

            # Obtener temas ordenados por el campo 'orden'
            g.temas = TemaCurso.objects.filter(
                grupo_teoria=g.grupo_teoria
            ).order_by("orden")
            
            # === AUTO-MARCAR TEMAS CON 7+ DÍAS DE ANTIGÜEDAD ===
            hoy = date.today()

            for tema in g.temas:
                # Comprobación de que tema.fecha no sea None
                if tema.fecha and tema.fecha <= hoy - timedelta(days=7) and not tema.completado:
                    # Se requiere guardar cada tema individualmente si se modifica en el bucle
                    # Esto garantiza que el cambio se persista inmediatamente.
                    tema.completado = True
                    tema.save()
        elif g.laboratorio:
            g.tipo = "Laboratorio"
            g.grupo_teoria = None
            g.temas = []
        else:
            g.tipo = "Desconocido"
            g.grupo_teoria = None
            g.temas = []

        cursos_procesados.append(g)

    # ---------------------------------------------------------
    # 2. POST
    # ---------------------------------------------------------
    if request.method == "POST":
        accion = request.POST.get("accion")
        grupo_id = request.POST.get("grupo_id")
        
        # Redirección de fallback en caso de error
        redirect_url_base = reverse('usuarios:mis_cursos_profesor')
        redirect_url_specific = f"{redirect_url_base}?curso={grupo_id}" if grupo_id else redirect_url_base

        try:
            grupo_obj = GrupoCurso.objects.get(id=grupo_id)
        except GrupoCurso.DoesNotExist:
            messages.error(request, "Grupo de curso no encontrado.")
            return redirect(redirect_url_base)

        grupo_teoria = GrupoTeoria.objects.filter(grupo_curso=grupo_obj).first()

        # Si no es teoría → evitar error en acciones específicas de teoría
        if accion in ["registrar_tema", "marcar_completado", "borrar_tema", "cargar_excel"] and not grupo_teoria:
            messages.error(request, "Esta acción solo está disponible para grupos de Teoría.")
            return redirect(redirect_url_specific)

        # SUBIR SILABO
        if accion == "subir_silabo":
            archivo = request.FILES.get("silabo")
            if archivo and archivo.name.endswith(".pdf"):
                # Simulación de subida: Guardamos el nombre del archivo como URL para este ejemplo
                # En un entorno real, aquí se llamaría a un servicio de almacenamiento (AWS S3, Google Cloud Storage, etc.)
                try:
                    curso = grupo_obj.curso
                    # Usar el nombre del archivo subido como URL temporal
                    curso.silabo_url = f"/media/silabos/{curso.id}_{grupo_obj.grupo}_{archivo.name}" 
                    curso.save()
                    messages.success(request, f"Sílabo '{archivo.name}' subido con éxito.")
                except Exception as e:
                    messages.error(request, f"Error al guardar el sílabo en la base de datos: {e}")
            else:
                messages.error(request, "Solo se permiten archivos PDF.")

            return redirect(redirect_url_specific)
            
        # ========================================================
        # NUEVA LÓGICA: ELIMINAR SILABO
        # ========================================================
        if accion == "eliminar_silabo":
            try:
                curso = grupo_obj.curso
                # En una aplicación real, aquí también se debería eliminar el archivo del sistema de almacenamiento.
                if curso.silabo_url:
                    nombre_archivo_previo = curso.silabo_url.split('/')[-1]
                    curso.silabo_url = None # Establecer el campo a null/None
                    curso.save()
                    messages.success(request, f"Sílabo '{nombre_archivo_previo}' eliminado con éxito.")
                else:
                    messages.warning(request, "No hay sílabo cargado para eliminar.")
            except Exception as e:
                messages.error(request, f"Error al intentar eliminar el sílabo: {e}")

            return redirect(redirect_url_specific)

        # REGISTRO DE TEMA
        if accion == "registrar_tema":
            nombre = request.POST.get("nombre")
            fecha = request.POST.get("fecha")
            
            # Se asegura que el orden sea secuencial
            orden = TemaCurso.objects.filter(grupo_teoria=grupo_teoria).count() + 1

            if nombre and fecha:
                try:
                    TemaCurso.objects.create(
                        nombre=nombre,
                        fecha=fecha,
                        orden=orden,
                        completado=False,
                        grupo_teoria=grupo_teoria
                    )
                    messages.success(request, f"Tema '{nombre}' registrado con éxito.")
                except IntegrityError:
                     messages.error(request, "Error de integridad al registrar el tema.")
                except Exception as e:
                     messages.error(request, f"Error desconocido al registrar el tema: {e}")
            else:
                messages.error(request, "Faltan datos (Nombre o Fecha) para registrar el tema.")

            return redirect(redirect_url_specific)

        # MARCAR COMPLETADO
        if accion == "marcar_completado":
            tema_id = request.POST.get("tema_id")
            try:
                tema = TemaCurso.objects.get(id=tema_id)
                # Solo permite marcar/desmarcar si pertenece al grupo de teoría correcto
                if tema.grupo_teoria == grupo_teoria:
                    tema.completado = not tema.completado
                    tema.save()
                    messages.success(request, f"Tema '{tema.nombre}' actualizado.")
                else:
                    messages.error(request, "El tema no pertenece a este grupo de teoría.")
            except TemaCurso.DoesNotExist:
                messages.error(request, "Tema no encontrado.")

            return redirect(redirect_url_specific)

        # BORRAR TEMA
        if accion == "borrar_tema":
            tema_id = request.POST.get("tema_id")
            try:
                # Usamos transaction.atomic para asegurar que el borrado y el reordenamiento sean atómicos
                with transaction.atomic():
                    TemaCurso.objects.filter(id=tema_id, grupo_teoria=grupo_teoria).delete() # Se añade filtro por grupo_teoria por seguridad
                    messages.success(request, "Tema eliminado con éxito.")

                    # Reordenar: Se corrige el ordenamiento para que sea por el campo 'orden'
                    temas = TemaCurso.objects.filter(grupo_teoria=grupo_teoria).order_by("orden")
                    for i, t in enumerate(temas, start=1):
                        t.orden = i
                        t.save()
            except Exception as e:
                messages.error(request, f"Error al intentar borrar/reordenar el tema: {e}")


            return redirect(redirect_url_specific)

        # CARGA MASIVA
        if accion == "cargar_excel":
            archivo = request.FILES.get("archivo")

            if archivo is None:
                messages.error(request, "Debe seleccionar un archivo para la carga masiva.")
                return redirect(redirect_url_specific)
            
            try:
                # 1. Obtener GrupoTeoria (solo si es Teoría)
                try:
                    # Asume que GrupoTeoria tiene un FK o OneToOneField a GrupoCurso
                    grupo_teoria_obj = GrupoTeoria.objects.get(grupo_curso=grupo_obj)
                except GrupoTeoria.DoesNotExist:
                    messages.error(request, "Solo puede cargar temas masivamente en grupos de Teoría.")
                    return redirect(redirect_url_specific)

                # 2. Leer el archivo (soporta CSV y XLSX)
                file_extension = archivo.name.split('.')[-1].lower()
                df = None
                
                if file_extension == "csv":
                    # Usar StringIO para leer el archivo CSV
                    df = pd.read_csv(io.StringIO(archivo.read().decode('utf-8')), header=None)
                elif file_extension == "xlsx":
                    # Usar io.BytesIO para leer el archivo XLSX
                    df = pd.read_excel(io.BytesIO(archivo.read()), header=None)
                else:
                    messages.error(request, "Formato de archivo no soportado. Use .csv o .xlsx.")
                    return redirect(redirect_url_specific)
                
                # 3. Validar columnas
                if df.shape[1] < 2:
                    messages.error(request, "El archivo debe tener al menos dos columnas (Nombre del Tema y Fecha).")
                    return redirect(redirect_url_specific)
                
                # 4. Procesar y guardar en una transacción
                temas_creados = 0
                errores = []

                # Obtener el último orden para continuar la numeración
                ultimo_tema = TemaCurso.objects.filter(
                    grupo_teoria=grupo_teoria_obj
                ).order_by('-orden').first()
                
                orden_actual = ultimo_tema.orden + 1 if ultimo_tema else 1

                with transaction.atomic():
                    # df.iterrows() genera (index, Series)
                    for index, row in df.iterrows():
                        try:
                            # Columna 0: Nombre del Tema
                            nombre_tema = str(row.iloc[0]).strip()
                            if not nombre_tema: continue # Saltar filas vacías
                            
                            # Columna 1: Fecha (se intenta parsear robustamente)
                            fecha_str = str(row.iloc[1]).strip()
                            
                            # Intentar parsear la fecha, 'coerce' convierte fallos a NaT
                            fecha_dt = pd.to_datetime(fecha_str, errors='coerce')
                            
                            if pd.isna(fecha_dt):
                                raise ValueError(f"Formato de fecha inválido: {fecha_str}")
                                
                            fecha_obj = fecha_dt.date()
                            
                            # Crear el objeto TemaCurso
                            TemaCurso.objects.create(
                                grupo_teoria=grupo_teoria_obj,
                                nombre=nombre_tema,
                                fecha=fecha_obj,
                                orden=orden_actual,
                                completado=False
                            )
                            orden_actual += 1
                            temas_creados += 1
                            
                        except ValueError as ve:
                            errores.append(f"Fila {index + 1} ('{nombre_tema}'): {ve}")
                        except Exception as e:
                            errores.append(f"Fila {index + 1} ('{nombre_tema}'): Error desconocido al procesar la fila. {e}")

                if errores:
                    # Mostrar errores y el número de temas creados exitosamente
                    error_msg = f"Carga masiva completada con {temas_creados} temas creados, pero se encontraron {len(errores)} errores en el archivo. "
                    messages.warning(request, error_msg + "Detalles: " + "; ".join(errores[:3]) + ("..." if len(errores) > 3 else "")) 
                else:
                    messages.success(request, f"Carga masiva exitosa. Se registraron {temas_creados} temas.")

            except Exception as e:
                messages.error(request, f"Error general durante la carga masiva: {e}. Asegúrese de que el archivo no esté vacío y el formato de fecha sea estándar (YYYY-MM-DD).")

            return redirect(redirect_url_specific)

    # ---------------------------------------------------------
    # 3. Render
    # ---------------------------------------------------------
    curso_seleccionado = request.GET.get("curso", None)

    return render(request, "usuarios/profesor/mis_cursos_profesor.html", {
        "perfil": profesor_obj.perfil,
        "titulo": "Carga Académica",
        "carga_academica": cursos_procesados,
        "periodo": "2025-II",
        "curso_seleccionado": curso_seleccionado,
    })

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

        duracion_minutos = (dt_fin_fila - dt_inicio_fila).total_seconds() / 60
        if duracion_minutos < 11:
            hora_actual = dt_siguiente
            continue
        
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
    if response:
        return response

    # SOLO GRUPOS DE TEORÍA
    grupos = GrupoCurso.objects.filter(
        profesor=profesor_obj,
        grupoteoria__isnull=False
    ).select_related("curso")

    # ----------------------------
    # BORRAR DOCUMENTO (POST)
    # ----------------------------
    if request.method == "POST" and "delete_campo" in request.POST:
        curso_id = request.POST.get("curso_id")
        campo = request.POST.get("delete_campo")

        curso = Curso.objects.filter(id=curso_id).first()
        if not curso or not hasattr(curso, campo):
            messages.error(request, "No se pudo borrar el archivo.")
            return redirect("usuarios:acreditacion")

        setattr(curso, campo, None)
        curso.save()
        messages.success(request, "Archivo eliminado correctamente.")
        return redirect("usuarios:acreditacion")

    # ----------------------------
    # SUBIDA DE DOCUMENTO
    # ----------------------------
    if request.method == "POST" and "documento" in request.FILES:
        curso_id = request.POST.get("curso_id")
        fase = request.POST.get("fase")
        tipo = request.POST.get("tipo")
        archivo = request.FILES.get("documento")

        grupo = GrupoCurso.objects.filter(
            profesor=profesor_obj,
            curso__id=curso_id,
            grupoteoria__isnull=False
        ).select_related("curso").first()

        if not grupo:
            messages.error(request, "Ese curso no le pertenece o no es teoría.")
            return redirect("usuarios:acreditacion")

        curso = grupo.curso

        campo = f"{fase}{tipo}_url"  # ej: Fase1notaAlta_url
        setattr(curso, campo, archivo.name)
        curso.save()

        messages.success(request, "Documento subido correctamente.")
        return redirect("usuarios:acreditacion")

    # ----------------------------
    # ARMAR TABLA DE DOCUMENTOS
    # ----------------------------
    documentos_por_curso = []

    for g in grupos:
        curso = g.curso

        fases = []
        for f in ["Fase1", "Fase2", "Fase3"]:
            tipos = []

            for t in ["notaAlta", "notaMedia", "notaBaja"]:
                campo = f"{f}{t}_url"
                tipos.append({
                    "campo": campo,
                    "nombre": t.replace("nota", "Nota "),
                    "archivo": getattr(curso, campo),
                })

            fases.append({
                "fase": f,
                "tipos": tipos,
            })

        documentos_por_curso.append({
            "grupo": g,
            "curso": curso,
            "fases": fases,
        })

    contexto = {
        "perfil": profesor_obj.perfil,
        "titulo": "Acreditación",
        "cursos_docente": grupos,
        "documentos": documentos_por_curso,
    }

    return render(request, 'usuarios/profesor/acreditacion.html', contexto)

def get_client_ip(request):
    """Obtiene IP real teniendo en cuenta proxy reverso si aplica."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '127.0.0.1')

def registro_asistencia(request):
    # 1. Autenticación del profesor y obtención del objeto profesor
    profesor_obj, response = check_professor_auth(request)
    if response:
        return response

    grupo_id = request.GET.get('grupo')
    fecha_q = request.GET.get('fecha')
    hoy = dt.date.today()
    fecha_actual_str = hoy.isoformat()
    
    # Mapear el día de la semana de Python (0=Lunes, 1=Martes...) a los choices de Django
    dia_semana_choices = {
        0: 'LUNES', 1: 'MARTES', 2: 'MIERCOLES',
        3: 'JUEVES', 4: 'VIERNES', 5: 'SABADO', 6: 'DOMINGO' # Incluyo fin de semana por si acaso
    }
    # Solo buscamos bloques si es un día laborable (L-V)
    dia_actual_str = dia_semana_choices.get(hoy.weekday(), None)

    # ----------------------------------------------------------------------
    # LÓGICA DE CÁLCULO DE RESERVAS ACTIVAS HASTA EL VIERNES (Solicitud anterior)
    # ----------------------------------------------------------------------
    days_until_friday = (4 - hoy.weekday() + 7) % 7 
    fecha_viernes = hoy + dt.timedelta(days=days_until_friday)

    reservas_activas_semana = Reserva.objects.filter(
        profesor=profesor_obj,
        fecha_reserva__range=[hoy, fecha_viernes] 
    ).count()
    # ----------------------------------------------------------------------

    # 2. LÓGICA DE CARGA REAL DE GRUPOS ASIGNADOS
    # **IMPORTANTE:** Usar select_related('curso') para verificar el sílabo eficientemente
    grupos_asignados = GrupoCurso.objects.filter(profesor=profesor_obj).select_related('curso').order_by('curso__nombre', 'grupo')

    # --- LÓGICA PARA RESALTAR GRUPOS CON CLASE HOY Y VERIFICAR SILABO ---
    grupos_con_clase_hoy_ids = set()
    
    if dia_actual_str in ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']:
        grupos_con_clase_hoy_ids = set(
            BloqueHorario.objects.filter(
                grupo_curso__in=grupos_asignados.values('id'),
                dia=dia_actual_str
            ).values_list('grupo_curso_id', flat=True).distinct()
        )

    for g in grupos_asignados:
        # Determinar el tipo
        if GrupoTeoria.objects.filter(grupo_curso=g).exists():
            g.tipo = "TEORÍA"
        elif GrupoLaboratorio.objects.filter(grupo_curso=g).exists():
            g.tipo = "LAB"
        else:
            g.tipo = "?"
            
        # Determinar si tiene clase hoy
        g.tiene_clase_hoy = g.id in grupos_con_clase_hoy_ids
        
        # **NUEVA VERIFICACIÓN DE SÍLABO**
        # Añade un atributo dinámico al objeto GrupoCurso para usar en el template
        g.silabo_subido = bool(g.curso.silabo_url)
        # ---------------------------------------------------

    grupo_seleccionado = None
    estudiantes_list = []
    historial_completo = []

    # POST (Las acciones POST (guardar/exportar) asumen que el sílabo ya fue subido si llegaron aquí)
    if request.method == "POST":
        accion = request.POST.get('accion')

        # --- AJAX SAVE (Guardado individual) ---
        if accion == "ajax_save":
            # ... (Lógica de AJAX SAVE, sin cambios significativos) ...
            if request.headers.get('x-requested-with') != 'XMLHttpRequest':
                return HttpResponseBadRequest("Only AJAX")
            
            grupo_id_post = request.POST.get('grupo_id')
            fecha_post = request.POST.get('fecha')
            estudiante_cui = request.POST.get('estudiante_cui')
            estado_simple = request.POST.get('estado')

            if not (grupo_id_post and fecha_post and estudiante_cui and estado_simple in ('A', 'F')):
                return JsonResponse({'ok': False, 'msg': 'Faltan datos.'}, status=400)
            
            try:
                grupo_obj = GrupoCurso.objects.select_related('curso').get(id=grupo_id_post, profesor=profesor_obj)
            except GrupoCurso.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Grupo inválido.'}, status=403)
            
            # **RESTRICCIÓN DE SÍLABO EN POST/AJAX**
            if not grupo_obj.curso.silabo_url:
                return JsonResponse({'ok': False, 'msg': 'RESTRICCIÓN: Sílabo pendiente de subir.'}, status=403)

            # Solo se permite edición por AJAX para la fecha actual
            if fecha_post != hoy.isoformat():
                return JsonResponse({'ok': False, 'msg': 'Solo edición para la fecha actual permitida por AJAX.'}, status=403)
            
            try:
                fecha_obj = dt.date.fromisoformat(fecha_post)
            except ValueError:
                return JsonResponse({'ok': False, 'msg': 'Formato de fecha inválido.'}, status=400)
            
            try:
                from usuarios.models import Estudiante # Importar Estudiante si no está arriba
                estudiante_obj = Estudiante.objects.get(perfil__id=estudiante_cui)
            except Estudiante.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Estudiante no encontrado.'}, status=404)
            
            registro_principal, _ = RegistroAsistencia.objects.get_or_create(
                grupo_curso=grupo_obj,
                fechaClase=fecha_obj,
                defaults={'ipProfesor': get_client_ip(request), 'horaInicioVentana': timezone.now().time()}
            )
            
            estado_model = 'PRESENTE' if estado_simple == 'A' else 'FALTA'
            
            RegistroAsistenciaDetalle.objects.update_or_create(
                registro_asistencia=registro_principal,
                estudiante=estudiante_obj,
                defaults={'estado': estado_model}
            )
            
            return JsonResponse({'ok': True, 'msg': 'Asistencia guardada con éxito.'})


        # --- SAVE ALL (Guardado masivo) ---
        elif accion == "save_all":
            grupo_id_post = request.POST.get('grupo_id')
            fecha_post = request.POST.get('fecha_sesion')

            if not (grupo_id_post and fecha_post):
                messages.error(request, "Faltan datos esenciales (Grupo o Fecha).")
                return redirect('usuarios:registro_asistencia')

            try:
                # Obtener el grupo con el curso relacionado para la verificación del sílabo
                grupo_obj = GrupoCurso.objects.select_related('curso').get(id=grupo_id_post, profesor=profesor_obj)
            except GrupoCurso.DoesNotExist:
                messages.error(request, "Grupo inválido o no asignado a usted.")
                return redirect('usuarios:registro_asistencia')
            
            # **RESTRICCIÓN DE SÍLABO EN POST/SAVE_ALL**
            if not grupo_obj.curso.silabo_url:
                messages.error(request, f"¡RESTRICCIÓN! Debe subir el sílabo para el curso {grupo_obj.curso.nombre} antes de poder guardar la asistencia.")
                return redirect(f"{reverse('usuarios:registro_asistencia')}?grupo={grupo_id_post}&fecha={fecha_post}")


            try:
                fecha_obj = dt.date.fromisoformat(fecha_post)
            except ValueError:
                messages.error(request, "Formato de fecha inválido.")
                return redirect('usuarios:registro_asistencia')
            
            grupo_lab = GrupoLaboratorio.objects.filter(grupo_curso=grupo_obj).first()
            
            # ... (Resto de la lógica de guardado masivo, sin cambios) ...
            if grupo_lab:
                # Es Laboratorio, usamos MatriculaLaboratorio
                matriculas_query = MatriculaLaboratorio.objects.filter(
                    laboratorio=grupo_lab
                ).select_related('estudiante__perfil')
            else:
                # Es Teoría/General, usamos Matricula
                matriculas_query = Matricula.objects.filter(
                    grupo_curso=grupo_obj
                ).select_related('estudiante__perfil')

            # 1. Obtener o crear el registro principal
            registro_principal, created = RegistroAsistencia.objects.get_or_create(
                grupo_curso=grupo_obj,
                fechaClase=fecha_obj,
                defaults={'ipProfesor': get_client_ip(request), 'horaInicioVentana': timezone.now().time()}
            )

            # 2. Obtener la lista de estudiantes matriculados
            matriculas = matriculas_query
            updated = 0

            with transaction.atomic():
                for m in matriculas:
                    cui = str(m.estudiante.perfil.id)
                    
                    # CORRECCIÓN: Leer los nombres de campo correctos ('asistencia_{cui}' y 'falta_{cui}')
                    asistencia_post = request.POST.get(f"asistencia_{cui}") 
                    falta_post = request.POST.get(f"falta_{cui}") 
                    
                    if asistencia_post == "A":
                        estado_model = "PRESENTE"
                    elif falta_post == "F":
                        estado_model = "FALTA"
                    else:
                        estado_model = "FALTA" # Default si ambos están desmarcados

                    RegistroAsistenciaDetalle.objects.update_or_create(
                        registro_asistencia=registro_principal,
                        estudiante=m.estudiante,
                        defaults={'estado': estado_model}
                    )
                    updated += 1

            messages.success(request, f"Asistencia guardada exitosamente para {updated} estudiantes en la fecha {fecha_post}.")
            return redirect(f"{reverse('usuarios:registro_asistencia')}?grupo={grupo_id_post}&fecha={fecha_post}")


        # --- LÓGICA DE EXPORTACIÓN (Excel y PDF) ---
        elif accion in ["export_excel", "export_pdf"]:
            grupo_id_export = request.POST.get('grupo_export')

            if not grupo_id_export:
                messages.error(request, "Falta el ID del grupo para exportar.")
                return redirect('usuarios:registro_asistencia')

            try:
                grupo_obj = GrupoCurso.objects.select_related('curso').get(id=grupo_id_export, profesor=profesor_obj)
            except GrupoCurso.DoesNotExist:
                messages.error(request, "Grupo inválido o no asignado a usted.")
                return redirect('usuarios:registro_asistencia')
            
            # **RESTRICCIÓN DE SÍLABO EN POST/EXPORT**
            if not grupo_obj.curso.silabo_url:
                messages.error(request, f"¡RESTRICCIÓN! Debe subir el sílabo para el curso {grupo_obj.curso.nombre} antes de poder exportar el registro.")
                return redirect(f"{reverse('usuarios:registro_asistencia')}?grupo={grupo_id_export}")

            # 1. Obtener los datos necesarios:
            # (Resto de la lógica de exportación, sin cambios significativos)
            
            # Determinar si es un grupo de Laboratorio
            grupo_lab_export = GrupoLaboratorio.objects.filter(grupo_curso=grupo_obj).first()

            if grupo_lab_export:
                matriculas_query = MatriculaLaboratorio.objects.filter(
                    laboratorio=grupo_lab_export
                )
            else:
                matriculas_query = Matricula.objects.filter(
                    grupo_curso=grupo_obj
                )
            
            asistencias_sesiones = RegistroAsistencia.objects.filter(grupo_curso=grupo_obj).order_by('fechaClase')
            fechas_sesiones = [a.fechaClase for a in asistencias_sesiones]
            fechas_str = [f.strftime('%Y-%m-%d') for f in fechas_sesiones]

            # Obtener estudiantes de la matrícula y su perfil
            if grupo_lab_export:
                estudiantes = [{'cui': m.estudiante.perfil.id, 'nombre': m.estudiante.perfil.nombre} 
                               for m in matriculas_query.select_related('estudiante__perfil').order_by('estudiante__perfil__nombre')]
            else:
                estudiantes = [{'cui': m.estudiante.perfil.id, 'nombre': m.estudiante.perfil.nombre} 
                               for m in matriculas_query.select_related('estudiante__perfil').order_by('estudiante__perfil__nombre')]

            detalles = RegistroAsistenciaDetalle.objects.filter(
                registro_asistencia__in=asistencias_sesiones
            ).select_related('estudiante__perfil', 'registro_asistencia')

            # Diccionario de pivote: { CUI: { 'YYYY-MM-DD': 'A'/'F' } }
            pivote_asistencia = {}
            for d in detalles:
                cui = d.estudiante.perfil.id
                fecha_str = d.registro_asistencia.fechaClase.strftime('%Y-%m-%d')
                estado = 'A' if d.estado == 'PRESENTE' else 'F'

                if cui not in pivote_asistencia:
                    pivote_asistencia[cui] = {}
                pivote_asistencia[cui][fecha_str] = estado

            # 2. Construir la estructura final para el reporte
            reporte_data = []
            for est in estudiantes:
                fila = {
                    'cui': est['cui'],
                    'nombre': est['nombre'],
                    'registros': {}
                }
                for f_str in fechas_str:
                    fila['registros'][f_str] = pivote_asistencia.get(est['cui'], {}).get(f_str, '') 
                reporte_data.append(fila)
                
            # --- Generación de EXCEL (Openpyxl) ---
            if accion == "export_excel":
                output = io.BytesIO()
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.title = "Registro Asistencia"

                header_style = openpyxl.styles.Font(bold=True)

                headers = ['CUI', 'ALUMNO'] + [dt.datetime.strptime(f, '%Y-%m-%d').strftime('%d/%m') for f in fechas_str]
                sheet.append(headers)
                
                for cell in sheet[1]:
                    cell.font = header_style

                for fila in reporte_data:
                    row_data = [fila['cui'], fila['nombre']] + list(fila['registros'].values())
                    sheet.append(row_data)

                workbook.save(output)
                output.seek(0)
                
                response = HttpResponse(
                    output.read(),
                    content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="asistencia_{grupo_obj.curso.nombre}_{grupo_obj.grupo}.xlsx"'},
                )
                return response
            
            # --- Generación de PDF (xhtml2pdf) ---
            elif accion == "export_pdf":
                
                # 1. Renderizar el HTML de la tabla usando el template
                html_content = render_to_string('usuarios/profesor/reporte_asistencia_pdf.html', {
                    'grupo': grupo_obj,
                    'fechas': [dt.datetime.strptime(f, '%Y-%m-%d').strftime('%d/%m/%Y') for f in fechas_str],
                    'reporte_data': reporte_data,
                    'titulo_reporte': f"Reporte de Asistencia: {grupo_obj.curso.nombre} (Grupo {grupo_obj.grupo})",
                    'fecha_generacion': hoy.strftime('%d/%m/%Y')
                })
                
                # Creamos un buffer en memoria para almacenar el PDF binario
                pdf_file_buffer = io.BytesIO()
                
                # 2. Convertir el HTML a PDF usando xhtml2pdf (pisa)
                try:
                    pisa_status = pisa.CreatePDF(
                        html_content, # El contenido HTML/CSS a convertir
                        dest=pdf_file_buffer # Donde escribir el PDF (el buffer)
                    )
                except Exception as e:
                    error_msg = f"Error al generar PDF con xhtml2pdf: {e}. Verifique que el formato HTML/CSS sea compatible."
                    messages.error(request, error_msg)
                    return redirect(f"{reverse('usuarios:registro_asistencia')}?grupo={grupo_id_export}")

                if pisa_status.err:
                    error_msg = "Error interno de xhtml2pdf al generar el PDF. Revise el formato del template HTML."
                    messages.error(request, error_msg)
                    return redirect(f"{reverse('usuarios:registro_asistencia')}?grupo={grupo_id_export}")

                pdf_file = pdf_file_buffer.getvalue()
                pdf_file_buffer.close()
                
                # 3. Crear la respuesta HTTP para el PDF
                response = HttpResponse(pdf_file, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="asistencia_{grupo_obj.curso.nombre}_{grupo_obj.grupo}.pdf"'
                
                return response


    # GET (Lógica de carga inicial y filtro)
    if grupo_id:
        try:
            # Reutilizar el grupo ya procesado de grupos_asignados
            # Se convierte a str el id del grupo.
            grupo_seleccionado = next(g for g in grupos_asignados if str(g.id) == grupo_id)
        except StopIteration:
            messages.error(request, "Grupo no encontrado o no asignado a usted.")
            grupo_seleccionado = None

    if grupo_seleccionado:
        # **RESTRICCIÓN DE SÍLABO EN GET**
        if not grupo_seleccionado.silabo_subido:
            # Si el sílabo falta, mostramos un error de Django y evitamos cargar la lista de estudiantes/historial.
            messages.error(request, f"¡RESTRICCIÓN! Debe subir el sílabo para el curso {grupo_seleccionado.curso.nombre} antes de poder tomar asistencia. Vaya a la sección de Acreditación.")
            # Limpiamos variables para que no se muestre el detalle de asistencia
            estudiantes_list = [] 
            historial_completo = []
            # Mantener grupo_seleccionado para el contexto del encabezado
        else:
            # Si el sílabo está subido, procedemos con la carga de datos de asistencia
            # Historial de Asistencias y Matriculas
            grupo_lab = GrupoLaboratorio.objects.filter(grupo_curso=grupo_seleccionado).first()

            if grupo_lab:
                # Es un grupo de Laboratorio, usamos MatriculaLaboratorio
                matriculas = MatriculaLaboratorio.objects.filter(
                    laboratorio=grupo_lab
                ).select_related('estudiante__perfil').order_by('estudiante__perfil__nombre')
            else:
                # Es un grupo de Teoría (o general), usamos Matricula
                matriculas = Matricula.objects.filter(
                    grupo_curso=grupo_seleccionado
                ).select_related('estudiante__perfil').order_by('estudiante__perfil__nombre')
                
            # Historial de Asistencias
            historial_completo = RegistroAsistencia.objects.filter(grupo_curso=grupo_seleccionado).annotate(
                presentes=Count('registroasistenciadetalle', filter=Q(registroasistenciadetalle__estado='PRESENTE')),
                faltas=Count('registroasistenciadetalle', filter=Q(registroasistenciadetalle__estado='FALTA'))
            ).order_by('-fechaClase')

            registro = None
            detalles_map = {}
            
            if fecha_q:
                try:
                    fecha_obj = dt.date.fromisoformat(fecha_q)
                except ValueError:
                    fecha_obj = None

                if fecha_obj:
                    registro = RegistroAsistencia.objects.prefetch_related(
                        Prefetch('registroasistenciadetalle_set', queryset=RegistroAsistenciaDetalle.objects.select_related('estudiante__perfil'))
                    ).filter(grupo_curso=grupo_seleccionado, fechaClase=fecha_obj).first()
                    
                    if registro:
                        for d in registro.registroasistenciadetalle_set.all():
                            detalles_map[d.estudiante.perfil.id] = d.estado

                
                default_estado_contexto = '' 
                # Si no hay registro Y estamos mirando la fecha de hoy, se sugiere 'PRESENTE' por defecto
                if not registro and fecha_q == fecha_actual_str:
                    default_estado_contexto = 'A'
                
                for m in matriculas:
                    # Determinar el objeto estudiante. Varía si es Matricula o MatriculaLaboratorio
                    from usuarios.models import Estudiante # Importar Estudiante si no está arriba
                    estudiante_obj = m.estudiante 
                    cui = estudiante_obj.perfil.id
                    
                    estado_db = detalles_map.get(cui) 
                    
                    if estado_db:
                        estado_contexto = 'A' if estado_db == 'PRESENTE' else 'F'
                    else:
                        estado_contexto = default_estado_contexto

                    estudiantes_list.append({
                        'cui': cui,
                        'nombre': estudiante_obj.perfil.nombre,
                        'estado': estado_contexto
                    })
            else:
                for m in matriculas:
                    from usuarios.models import Estudiante # Importar Estudiante si no está arriba
                    # Determinar el objeto estudiante. Varía si es Matricula o MatriculaLaboratorio
                    estudiante_obj = m.estudiante
                    estudiantes_list.append({
                        'cui': estudiante_obj.perfil.id,
                        'nombre': estudiante_obj.perfil.nombre,
                        'estado': '' 
                    })

    # 'editable' es True solo si se seleccionó la fecha de hoy
    editable = (fecha_q == fecha_actual_str) if fecha_q else False

    contexto = {
        'perfil': profesor_obj.perfil,
        'titulo': 'Registro de Asistencia',
        'grupos_asignados': grupos_asignados,
        'grupo_seleccionado': grupo_seleccionado,
        'fecha_url': fecha_q,
        'fecha_actual_str': fecha_actual_str,
        'editable': editable,
        'estudiantes_matriculados': estudiantes_list,
        'historial_completo': historial_completo,
        # Nueva variable que refleja la cuenta de reservas de la semana (hasta el viernes)
        'reservas_activas_semana': reservas_activas_semana, 
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
# 3. VISTAS DE LA SECRETARIA
# ----------------------------------------------------------------------
def check_secretaria_auth(request):
    """Función de ayuda para verificar la sesión y el rol de Secretaria."""
    if not request.session.get('is_authenticated') or request.session.get('usuario_rol') != 'SECRETARIA':
        messages.warning(request, "Acceso denegado o rol incorrecto.")
        return None, redirect('usuarios:selector_rol')
    
    usuario_id = request.session['usuario_id']
    try:
        secretaria_obj = Secretaria.objects.select_related('perfil').get(perfil_id=usuario_id)
        return secretaria_obj, None
    except Secretaria.DoesNotExist:
        messages.error(request, "Error: Datos de secretaria no encontrados.")
        return None, redirect('usuarios:logout')

def dashboard_secretaria(request):
    """Muestra la página de inicio del dashboard de Secretaría."""
    secretaria_obj, response = check_secretaria_auth(request)
    if response:
        return response
    
    from django.db.models import Count, Q, Sum, Avg
    from django.utils import timezone
    from datetime import date, timedelta
    
    # =============================================
    # 1. CONTEOS BÁSICOS
    # =============================================
    conteo_profesores = Profesor.objects.count()
    conteo_estudiantes = Estudiante.objects.count()
    conteo_cursos = Curso.objects.count()
    conteo_aulas = Aula.objects.count()
    
    # =============================================
    # 2. ESTADÍSTICAS DE MATRÍCULAS
    # =============================================
    # Total de matrículas activas (estado=True)
    total_matriculas = Matricula.objects.filter(estado=True).count()
    
    # Matrículas por tipo (Teoría vs Laboratorio)
    matriculas_teoria = Matricula.objects.filter(
        estado=True,
        grupo_curso__grupoteoria__isnull=False
    ).count()
    
    matriculas_lab = MatriculaLaboratorio.objects.count()
    
    # =============================================
    # 3. ESTADÍSTICAS DE HORARIOS
    # =============================================
    # Total de bloques de horario asignados
    total_bloques = BloqueHorario.objects.count()
    
    # Bloques por tipo de aula
    bloques_aula_normal = BloqueHorario.objects.filter(
        aula__tipo='AULA_NORMAL'
    ).count()
    
    bloques_laboratorio = BloqueHorario.objects.filter(
        aula__tipo='LABORATORIO'
    ).count()
    
    # =============================================
    # 4. ESTADÍSTICAS DE RESERVAS (semana actual: Lunes a Viernes)
    # =============================================
    hoy = date.today()
    
    # Calcular el lunes de la semana actual (0=Lunes, 6=Domingo)
    dias_hasta_lunes = hoy.weekday()  # 0 para lunes, 1 para martes, etc.
    lunes_semana_actual = hoy - timedelta(days=dias_hasta_lunes)
    
    # Calcular el viernes de esta semana
    dias_hasta_viernes = 4 - hoy.weekday()  # 0 si es viernes, positivo si es antes
    if dias_hasta_viernes < 0:
        # Si ya pasó el viernes, ir al viernes de la siguiente semana
        dias_hasta_viernes += 7
    viernes_semana_actual = hoy + timedelta(days=dias_hasta_viernes)
    
    # Para estadísticas de "Actividad Reciente", usamos desde el lunes de esta semana
    # hasta el viernes de esta semana
    fecha_inicio_semana = lunes_semana_actual
    fecha_fin_semana = viernes_semana_actual
    
    reservas_semana = Reserva.objects.filter(
        fecha_reserva__range=[fecha_inicio_semana, fecha_fin_semana]
    ).count()
    
    # Reservas para hoy
    reservas_hoy = Reserva.objects.filter(fecha_reserva=hoy).count()
    
    # =============================================
    # 5. ESTADÍSTICAS DE ASISTENCIA (semana actual: Lunes a Viernes)
    # =============================================
    asistencia_semana = RegistroAsistencia.objects.filter(
        fechaClase__range=[fecha_inicio_semana, fecha_fin_semana]
    ).count()
    
    # Porcentaje promedio de asistencia
    if asistencia_semana > 0:
        total_estudiantes_asistencia = RegistroAsistenciaDetalle.objects.filter(
            registro_asistencia__fechaClase__range=[fecha_inicio_semana, fecha_fin_semana]
        ).count()
        
        presentes = RegistroAsistenciaDetalle.objects.filter(
            registro_asistencia__fechaClase__range=[fecha_inicio_semana, fecha_fin_semana],
            estado='PRESENTE'
        ).count()
        
        porcentaje_asistencia = (presentes / total_estudiantes_asistencia * 100) if total_estudiantes_asistencia > 0 else 0
    else:
        porcentaje_asistencia = 0
    
    # =============================================
    # 6. INFORMACIÓN DE GRUPOS ACTIVOS
    # =============================================
    # Grupos con profesor asignado
    grupos_con_profesor = GrupoCurso.objects.filter(profesor__isnull=False).count()
    grupos_sin_profesor = GrupoCurso.objects.filter(profesor__isnull=True).count()
    
    # Distribución por tipo de grupo
    grupos_teoria = GrupoTeoria.objects.count()
    grupos_laboratorio = GrupoLaboratorio.objects.count()
    
    # =============================================
    # 7. TOP 5 CURSOS CON MÁS MATRÍCULAS
    # =============================================
    top_cursos = Curso.objects.annotate(
        total_matriculas=Count('grupocurso__matricula', filter=Q(grupocurso__matricula__estado=True))
    ).order_by('-total_matriculas')[:5]
    
    # =============================================
    # 8. AULAS MÁS UTILIZADAS
    # =============================================
    aulas_populares = Aula.objects.annotate(
        total_bloques=Count('bloquehorario')
    ).order_by('-total_bloques')[:5]
    
    # =============================================
    # 9. PROFESORES MÁS ACTIVOS (por cantidad de grupos)
    # =============================================
    profesores_activos = Profesor.objects.annotate(
        total_grupos=Count('grupocurso')
    ).order_by('-total_grupos')[:5]

    cantidad_profesores_con_grupos = Profesor.objects.annotate(
        total_grupos=Count('grupocurso')
    ).filter(total_grupos__gt=0).count()
    
    contexto = {
        'perfil': secretaria_obj.perfil,
        'titulo': 'Panel de Control - Secretaría',
        
        # Conteos básicos
        'conteo_profesores': conteo_profesores,
        'conteo_estudiantes': conteo_estudiantes,
        'conteo_cursos': conteo_cursos,
        'conteo_aulas': conteo_aulas,
        
        # Matrículas
        'total_matriculas': total_matriculas,
        'matriculas_teoria': matriculas_teoria,
        'matriculas_lab': matriculas_lab,
        
        # Horarios
        'total_bloques': total_bloques,
        'bloques_aula_normal': bloques_aula_normal,
        'bloques_laboratorio': bloques_laboratorio,
        
        # Reservas
        'reservas_semana': reservas_semana,
        'reservas_hoy': reservas_hoy,
        
        # Asistencia
        'asistencia_semana': asistencia_semana,
        'porcentaje_asistencia': round(porcentaje_asistencia, 1),
        
        # Grupos
        'grupos_con_profesor': grupos_con_profesor,
        'grupos_sin_profesor': grupos_sin_profesor,
        'grupos_teoria': grupos_teoria,
        'grupos_laboratorio': grupos_laboratorio,
        
        # Listados
        'top_cursos': top_cursos,
        'aulas_populares': aulas_populares,
        'profesores_activos': profesores_activos,
        'profesores_grupos_asignados': cantidad_profesores_con_grupos,
        
        # Fechas para referencia - Ahora muestra "Lunes Actual - Viernes Actual"
        'hoy': hoy.strftime('%d/%m/%Y'),
        'semana_pasada': fecha_inicio_semana.strftime('%d/%m/%Y'),  # Lunes de esta semana
        'viernes_semana': fecha_fin_semana.strftime('%d/%m/%Y'),    # Viernes de esta semana
    }
    
    return render(request, 'usuarios/secretaria/dashboard_secretaria.html', contexto)

def mi_cuenta_secretaria(request):
    # Autenticación
    secretaria_obj, response = check_secretaria_auth(request)
    if response: 
        return response

    perfil = secretaria_obj.perfil

    # -----------------------------
    # POST: cambio de contraseña
    # -----------------------------
    if request.method == 'POST':
        old_password = request.POST.get('old_password')
        new_password1 = request.POST.get('new_password1')
        new_password2 = request.POST.get('new_password2')

        # 1. Validar contraseña actual
        if old_password != perfil.password:
            messages.error(request, 'La contraseña actual ingresada es incorrecta.')
            return redirect('usuarios:mi_cuenta_secretaria')

        # 2. Validar nueva contraseña
        if new_password1 != new_password2:
            messages.error(request, 'La nueva contraseña y su confirmación no coinciden.')
            return redirect('usuarios:mi_cuenta_secretaria')

        if len(new_password1) < 6:
            messages.error(request, 'La nueva contraseña debe tener al menos 6 caracteres.')
            return redirect('usuarios:mi_cuenta_secretaria')

        if new_password1 == old_password:
            messages.warning(request, 'La nueva contraseña no puede ser igual a la anterior.')
            return redirect('usuarios:mi_cuenta_secretaria')

        # 3. Guardar
        try:
            perfil.password = new_password1
            perfil.save()
            messages.success(request, '¡Contraseña actualizada correctamente!')
        except Exception as e:
            messages.error(request, f'Error al guardar la nueva contraseña: {e}')

        return redirect('usuarios:mi_cuenta_secretaria')

    # -----------------------------
    # GET: mostrar página
    # -----------------------------
    contexto = {
        'perfil': perfil,
        'titulo': 'Mi Cuenta'
    }
    return render(request, 'usuarios/secretaria/mi_cuenta_secretaria.html', contexto)

def gestion_cursos(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: 
        return response

    # ======================================================================
    # 1. MANEJO DE SOLICITUDES AJAX (GET)
    # ======================================================================
    # Se usa para obtener dinámicamente los grupos de un curso y sus detalles (horarios, prof, etc.)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.method == 'GET':
        accion = request.GET.get('accion')
        
        # A. Obtener lista de grupos de teoría para el selector de edición
        if accion == 'obtener_grupos_curso':
            curso_id = request.GET.get('curso_id')
            # Filtramos solo aquellos que son Grupos de Teoría (tienen la relación grupoteoria)
            grupos = GrupoTeoria.objects.filter(grupo_curso__curso_id=curso_id).select_related('grupo_curso')
            data = [{'id': g.grupo_curso.id, 'grupo': g.grupo_curso.grupo} for g in grupos]
            return JsonResponse({'grupos': data})

        # B. Obtener el detalle completo de un grupo específico (Profesor, Capacidad, Horarios)
        if accion == 'obtener_detalle_grupo':
            grupo_id = request.GET.get('grupo_id')
            try:
                # Buscamos el GrupoCurso (que es la base)
                grupo_curso = GrupoCurso.objects.select_related('profesor__perfil').get(id=grupo_id)
                
                # Obtenemos sus bloques horarios
                bloques = BloqueHorario.objects.filter(grupo_curso=grupo_curso).select_related('aula')
                horarios = [{
                    'dia': b.dia,
                    'inicio': b.horaInicio.strftime('%H:%M:%S') if b.horaInicio else '',  # Agregar segundos
                    'fin': b.horaFin.strftime('%H:%M:%S') if b.horaFin else '',  # Agregar segundos
                    'aula_id': b.aula.id if b.aula else ''
                } for b in bloques]

                data = {
                    'id': grupo_curso.id,
                    'profesor_id': grupo_curso.profesor.perfil.id if grupo_curso.profesor else '',
                    'capacidad': grupo_curso.capacidad,
                    'horarios': horarios
                }
                return JsonResponse({'ok': True, 'data': data})
            except GrupoCurso.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Grupo no encontrado'})
            except Exception as e:
                return JsonResponse({'ok': False, 'msg': f'Error al obtener detalle: {str(e)}'})

        return JsonResponse({'ok': False, 'msg': 'Acción desconocida'})

    # ======================================================================
    # 2. MANEJO DE SOLICITUDES POST (CRUD)
    # ======================================================================
    if request.method == 'POST':
        accion = request.POST.get('accion')
        
        try:
            with transaction.atomic():
                
                # --- A. CREAR CURSO ---
                if accion == 'crear_curso':
                    Curso.objects.create(
                        id=request.POST.get('id'),
                        nombre=request.POST.get('nombre'),
                        creditos=request.POST.get('creditos'),
                        porcentajeEC1=request.POST.get('porcentajeEC1') or 0,
                        porcentajeEP1=request.POST.get('porcentajeEP1') or 0,
                        porcentajeEC2=request.POST.get('porcentajeEC2') or 0,
                        porcentajeEP2=request.POST.get('porcentajeEP2') or 0,
                        porcentajeEC3=request.POST.get('porcentajeEC3') or 0,
                        porcentajeEP3=request.POST.get('porcentajeEP3') or 0,
                        # URLs se inicializan en Null/Blank por defecto en el modelo
                    )
                    messages.success(request, "Curso creado exitosamente.")

                # --- B. EDITAR CURSO ---
                elif accion == 'editar_curso':
                    curso = get_object_or_404(Curso, id=request.POST.get('id'))
                    curso.nombre = request.POST.get('nombre')
                    curso.creditos = request.POST.get('creditos')
                    curso.porcentajeEC1 = request.POST.get('porcentajeEC1') or 0
                    curso.porcentajeEP1 = request.POST.get('porcentajeEP1') or 0
                    curso.porcentajeEC2 = request.POST.get('porcentajeEC2') or 0
                    curso.porcentajeEP2 = request.POST.get('porcentajeEP2') or 0
                    curso.porcentajeEC3 = request.POST.get('porcentajeEC3') or 0
                    curso.porcentajeEP3 = request.POST.get('porcentajeEP3') or 0
                    
                    # Lógica para manejar archivos subidos (AGREGAR ESTO)
                    # 1. Sílabo
                    if 'silabo' in request.FILES:
                        curso.silabo_url = request.FILES['silabo']
                    
                    # 2. Archivos de Fase 1
                    if 'fase1alta' in request.FILES:
                        curso.Fase1notaAlta_url = request.FILES['fase1alta']
                    if 'fase1media' in request.FILES:
                        curso.Fase1notaMedia_url = request.FILES['fase1media']
                    if 'fase1baja' in request.FILES:
                        curso.Fase1notaBaja_url = request.FILES['fase1baja']
                    
                    # 3. Archivos de Fase 2
                    if 'fase2alta' in request.FILES:
                        curso.Fase2notaAlta_url = request.FILES['fase2alta']
                    if 'fase2media' in request.FILES:
                        curso.Fase2notaMedia_url = request.FILES['fase2media']
                    if 'fase2baja' in request.FILES:
                        curso.Fase2notaBaja_url = request.FILES['fase2baja']
                    
                    # 4. Archivos de Fase 3
                    if 'fase3alta' in request.FILES:
                        curso.Fase3notaAlta_url = request.FILES['fase3alta']
                    if 'fase3media' in request.FILES:
                        curso.Fase3notaMedia_url = request.FILES['fase3media']
                    if 'fase3baja' in request.FILES:
                        curso.Fase3notaBaja_url = request.FILES['fase3baja']
                    
                    # Lógica para eliminar archivos si se marcaron los checkboxes (MANTENER ESTO)
                    if request.POST.get('eliminar_silabo') == '1': 
                        curso.silabo_url = None
                    if request.POST.get('eliminar_fase1alta') == '1': 
                        curso.Fase1notaAlta_url = None
                    if request.POST.get('eliminar_fase1media') == '1': 
                        curso.Fase1notaMedia_url = None
                    if request.POST.get('eliminar_fase1baja') == '1': 
                        curso.Fase1notaBaja_url = None
                    if request.POST.get('eliminar_fase2alta') == '1': 
                        curso.Fase2notaAlta_url = None
                    if request.POST.get('eliminar_fase2media') == '1': 
                        curso.Fase2notaMedia_url = None
                    if request.POST.get('eliminar_fase2baja') == '1': 
                        curso.Fase2notaBaja_url = None
                    if request.POST.get('eliminar_fase3alta') == '1': 
                        curso.Fase3notaAlta_url = None
                    if request.POST.get('eliminar_fase3media') == '1': 
                        curso.Fase3notaMedia_url = None
                    if request.POST.get('eliminar_fase3baja') == '1': 
                        curso.Fase3notaBaja_url = None
                    
                    curso.save()
                    messages.success(request, "Información del curso actualizada.")

                # --- C. ELIMINAR CURSO ---
                elif accion == 'eliminar_curso':
                    curso_id = request.POST.get('curso_id')
                    # Verificar si tiene grupos asignados antes de eliminar
                    if GrupoCurso.objects.filter(curso_id=curso_id).exists():
                        messages.error(request, "No se puede eliminar el curso debido a que hay grupos asignados.")
                    else:
                        Curso.objects.filter(id=curso_id).delete()
                        messages.success(request, "Curso eliminado correctamente.")

                # --- D. CREAR GRUPO TEORÍA ---
                elif accion == 'crear_grupo_teoria':
                    curso_id = request.POST.get('curso_id')
                    letra_grupo = request.POST.get('grupo', '').upper() # A, B, C...
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    # DEBUG: Mostrar lo que llega
                    print(f"DEBUG - horarios_json recibido: {horarios_json}")
                    
                    # Generación del ID Compuesto: CURSOID + LETRA
                    nuevo_id = f"{curso_id}{letra_grupo}"
                    
                    if GrupoCurso.objects.filter(id=nuevo_id).exists():
                        messages.error(request, f"El Grupo {letra_grupo} para este curso ya existe (ID: {nuevo_id}).")
                    else:
                        try:
                            # Validar horarios_json
                            if not horarios_json or horarios_json.strip() == '':
                                raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                            
                            # 1. Parsear JSON
                            horarios = json.loads(horarios_json)
                            
                            # Validar que haya al menos un horario
                            if not isinstance(horarios, list) or len(horarios) == 0:
                                raise ValueError("Debe agregar al menos un bloque de horario.")
                            
                            # 2. Iniciar Transacción Atómica
                            with transaction.atomic():
                                # Validación de existencia de objetos FK y conversión de capacidad
                                profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                                curso = Curso.objects.get(id=curso_id)
                                capacidad_int = int(capacidad)
                                
                                # 3. Validar cada horario
                                for i, h in enumerate(horarios, 1):
                                    try:
                                        aula_obj = Aula.objects.get(id=h['aula_id'])
                                    except ObjectDoesNotExist:
                                        raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                    
                                    # Validar campos requeridos
                                    if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                        raise ValueError(f"El horario {i} tiene campos incompletos.")
                                    
                                    # Convertir tiempos
                                    try:
                                        inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                        fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                    except ValueError:
                                        raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                    
                                    # Validar que inicio < fin
                                    if inicio_time >= fin_time:
                                        raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                    
                                    # 3.1 Verificar cruce con horarios existentes en la misma aula
                                    horarios_cruzados_aula = BloqueHorario.objects.filter(
                                        aula=aula_obj,
                                        dia=h['dia']
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_cruzados_aula.exists():
                                        conflicto = horarios_cruzados_aula.first()
                                        curso_conflicto = conflicto.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto.grupo_curso.grupo
                                        raise ValueError(
                                            f"Conflicto de horario en aula {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                            f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                        )
                                    
                                    # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                    if profesor:
                                        # Buscar horarios donde el docente ya tenga clases asignadas
                                        horarios_docente = BloqueHorario.objects.filter(
                                            grupo_curso__profesor=profesor,
                                            dia=h['dia']
                                        ).exclude(
                                            Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                        )
                                        
                                        if horarios_docente.exists():
                                            conflicto_docente = horarios_docente.first()
                                            curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                            grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                            aula_conflicto = conflicto_docente.aula.id
                                            raise ValueError(
                                                f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                                f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                            )

                                # 4. Crear el GrupoCurso base
                                nuevo_grupo = GrupoCurso.objects.create(
                                    id=nuevo_id,
                                    curso=curso,
                                    profesor=profesor,
                                    grupo=letra_grupo,
                                    capacidad=capacidad_int
                                )
                                
                                # 5. Crear la especialización GrupoTeoria
                                GrupoTeoria.objects.create(grupo_curso=nuevo_grupo)
                                
                                # 6. Crear los Bloques de Horario
                                for h in horarios:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                    BloqueHorario.objects.create(
                                        dia=h['dia'],
                                        horaInicio=h['inicio'],
                                        horaFin=h['fin'],
                                        grupo_curso=nuevo_grupo,
                                        aula=aula_obj
                                    )
                            
                            messages.success(request, f"Grupo de teoría {letra_grupo} creado exitosamente.")

                        except ObjectDoesNotExist as e:
                            messages.error(request, f"Error de referencia: {str(e)}")
                        except ValueError as e:
                            messages.error(request, f"Error en los datos: {str(e)}")
                        except json.JSONDecodeError as e:
                            messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                        except Exception as e:
                            messages.error(request, f"Error inesperado al crear el grupo: {str(e)}")

                # --- E. EDITAR GRUPO TEORÍA (GESTIONAR) ---
                elif accion == 'editar_grupo_teoria':
                    grupo_id = request.POST.get('grupo_id') # ID completo (Ej: INF101A)
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    # 1. Recuperar GrupoCurso o lanzar 404 si no existe
                    grupo_curso = get_object_or_404(GrupoCurso, id=grupo_id)
                    
                    try:
                        # Validar horarios_json
                        if not horarios_json or horarios_json.strip() == '':
                            raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                        
                        # Parsear JSON
                        horarios = json.loads(horarios_json)
                        
                        # Validar que haya al menos un horario
                        if not isinstance(horarios, list) or len(horarios) == 0:
                            raise ValueError("Debe agregar al menos un bloque de horario.")
                        
                        # 2. Iniciar Transacción Atómica
                        with transaction.atomic():
                            # Validación de existencia de objetos FK y conversión de capacidad
                            profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                            capacidad_int = int(capacidad) # Si no es un entero, lanza ValueError
                            
                            # 3. Validar cada horario antes de eliminar los existentes
                            for i, h in enumerate(horarios, 1):
                                try:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                except ObjectDoesNotExist:
                                    raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                
                                # Validar campos requeridos
                                if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                    raise ValueError(f"El horario {i} tiene campos incompletos.")
                                
                                # Convertir tiempos
                                try:
                                    inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                    fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                except ValueError:
                                    raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                
                                # Validar que inicio < fin
                                if inicio_time >= fin_time:
                                    raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                
                                # 3.1 Verificar cruce con horarios existentes en la misma aula
                                # Excluir los horarios del grupo actual que estamos editando
                                horarios_cruzados_aula = BloqueHorario.objects.filter(
                                    aula=aula_obj,
                                    dia=h['dia']
                                ).exclude(
                                    grupo_curso=grupo_curso  # Excluir los horarios del grupo actual
                                ).exclude(
                                    Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                )
                                
                                if horarios_cruzados_aula.exists():
                                    conflicto = horarios_cruzados_aula.first()
                                    curso_conflicto = conflicto.grupo_curso.curso.nombre
                                    grupo_conflicto = conflicto.grupo_curso.grupo
                                    raise ValueError(
                                        f"Conflicto de horario en aula {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                        f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                    )
                                
                                # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                if profesor:
                                    # Buscar horarios donde el docente ya tenga clases asignadas
                                    # Excluir los horarios del grupo actual que estamos editando
                                    horarios_docente = BloqueHorario.objects.filter(
                                        grupo_curso__profesor=profesor,
                                        dia=h['dia']
                                    ).exclude(
                                        grupo_curso=grupo_curso  # Excluir los horarios del grupo actual
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_docente.exists():
                                        conflicto_docente = horarios_docente.first()
                                        curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                        aula_conflicto = conflicto_docente.aula.id
                                        raise ValueError(
                                            f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                            f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                        )

                            # 4. Actualizar datos básicos
                            grupo_curso.profesor = profesor
                            grupo_curso.capacidad = capacidad_int
                            grupo_curso.save()
                            
                            # 5. Actualizar horarios: Estrategia de reemplazo total (Borrar y crear)
                            BloqueHorario.objects.filter(grupo_curso=grupo_curso).delete()
                            
                            for h in horarios:
                                aula_obj = Aula.objects.get(id=h['aula_id'])
                                BloqueHorario.objects.create(
                                    dia=h['dia'],
                                    horaInicio=h['inicio'],
                                    horaFin=h['fin'],
                                    grupo_curso=grupo_curso,
                                    aula=aula_obj
                                )
                        
                        messages.success(request, "Grupo actualizado correctamente.")
                    
                    except ObjectDoesNotExist as e:
                        # Captura error si el Profesor no se encuentra.
                        messages.error(request, f"Error de referencia: {str(e)}")
                    except ValueError as e:
                        # Captura errores de conversión (ej. capacidad no es int) o errores de Aula
                        messages.error(request, f"Error en los datos proporcionados: {str(e)}")
                    except json.JSONDecodeError as e:
                        messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                    except Exception as e:
                        # Captura errores genéricos, como JSON mal formado
                        messages.error(request, f"Error inesperado al actualizar el grupo: {str(e)}")

                # --- F. ELIMINAR GRUPO ---
                elif accion == 'eliminar_grupo':
                    grupo_id = request.POST.get('grupo_id')
                    # Al eliminar GrupoCurso, el CASCADE elimina GrupoTeoria y BloqueHorario
                    GrupoCurso.objects.filter(id=grupo_id).delete()
                    messages.success(request, "Grupo eliminado correctamente.")

        except IntegrityError as e:
            messages.error(request, f"Error de integridad en la base de datos: {str(e)}")
        except Exception as e:
            messages.error(request, f"Ocurrió un error inesperado: {str(e)}")
            
        return redirect('usuarios:gestion_cursos')

    # ======================================================================
    # 3. LÓGICA GET (RENDERIZADO DE LA PÁGINA)
    # ======================================================================
    
    # Obtener cursos con anotaciones para los contadores
    # num_grupos_teoria: Cuenta cuántos grupos tiene que sean de teoría
    # num_matriculados: Suma de matrículas en grupos que son de teoría
    cursos = Curso.objects.annotate(
        num_grupos_teoria=Count('grupocurso__grupoteoria', distinct=True),
        num_matriculados=Count('grupocurso__matricula', 
                               filter=Q(grupocurso__grupoteoria__isnull=False), 
                               distinct=True)
    ).order_by('id')

    # Listas auxiliares para llenar los selectores de los modales
    profesores_teoria = Profesor.objects.filter(es_teoria=True).select_related('perfil').order_by('perfil__nombre')
    aulas = Aula.objects.all().order_by('id')

    contexto = {
        'perfil': perfil_obj.perfil, 
        'titulo': 'Gestión de Cursos',
        'cursos': cursos,
        'profesores': profesores_teoria,
        'aulas': aulas
    }
    return render(request, 'usuarios/secretaria/gestion_cursos.html', contexto)

def ver_horarios_clases(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: 
        return response

    # ======================================================================
    # 1. MANEJO DE SOLICITUDES AJAX (GET) - Para cargar horarios por aula
    # ======================================================================
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.method == 'GET':
        accion = request.GET.get('accion')
        
        # Obtener horarios de un aula específica
        if accion == 'obtener_horarios_aula':
            aula_id = request.GET.get('aula_id')
            
            try:
                # Obtener el aula
                aula = Aula.objects.get(id=aula_id)
                
                # COLORES (excluyendo colores muy oscuros)
                COLOR_OPTIONS = [
                    'bg-primary', 'bg-success', 'bg-info', 
                    'bg-warning', 'bg-danger', 'bg-secondary'
                ]
                curso_colores = {}
                color_index = 0
                
                # Obtener todas las actividades para esta aula
                horario_completo = []
                puntos_corte = set()
                
                # 1. OBTENER CLASES REGULARES (GrupoCurso con BloqueHorario)
                bloques_clases = BloqueHorario.objects.filter(aula=aula).select_related(
                    'grupo_curso__curso',
                    'grupo_curso__profesor__perfil'
                ).order_by('horaInicio')
                
                print(f"DEBUG - Total bloques encontrados: {bloques_clases.count()}")
                
                for bloque in bloques_clases:
                    # Determinar tipo: Teoría o Laboratorio
                    tipo = "CLS"  # Por defecto
                    try:
                        # Verificar si es grupo de teoría
                        bloque.grupo_curso.grupoteoria
                        tipo = "TEO"
                    except:
                        try:
                            # Verificar si es grupo de laboratorio
                            bloque.grupo_curso.grupolaboratorio
                            tipo = "LAB"
                        except:
                            tipo = "CLS"  # Clase genérica
                    
                    # Profesor info
                    profesor_nombre = ""
                    if bloque.grupo_curso.profesor and bloque.grupo_curso.profesor.perfil:
                        profesor_nombre = bloque.grupo_curso.profesor.perfil.nombre
                    
                    print(f"DEBUG - Bloque: {bloque.grupo_curso.curso.nombre} - {bloque.dia} - {bloque.horaInicio} a {bloque.horaFin} - Tipo: {tipo}")
                    
                    # Agregar al horario completo
                    horario_completo.append({
                        'tipo_actividad': 'CLASE',
                        'bloque': bloque,
                        'horaInicio': bloque.horaInicio,
                        'horaFin': bloque.horaFin,
                        'dia': bloque.dia,
                        'curso': bloque.grupo_curso.curso,
                        'profesor_nombre': profesor_nombre,
                        'grupo': bloque.grupo_curso.grupo,
                        'tipo': tipo
                    })
                    
                    # Agregar puntos de corte (sin segundos/microsegundos)
                    puntos_corte.add(bloque.horaInicio.replace(second=0, microsecond=0))
                    puntos_corte.add(bloque.horaFin.replace(second=0, microsecond=0))
                    
                    # Asignar color al curso
                    curso_id = bloque.grupo_curso.curso.id
                    if curso_id not in curso_colores:
                        curso_colores[curso_id] = COLOR_OPTIONS[color_index % len(COLOR_OPTIONS)]
                        color_index += 1
                
                # 2. OBTENER RESERVAS PARA ESTA AULA
                # Obtener fecha actual para filtrar reservas de esta semana
                hoy = date.today()
                inicio_semana = hoy - timedelta(days=hoy.weekday())  # Lunes
                fin_semana = inicio_semana + timedelta(days=4)  # Viernes
                
                reservas = Reserva.objects.filter(
                    aula=aula,
                    fecha_reserva__range=[inicio_semana, fin_semana]
                ).select_related('profesor__perfil')
                
                print(f"DEBUG - Total reservas encontradas: {reservas.count()}")
                
                for reserva in reservas:
                    # Profesor info
                    profesor_nombre = ""
                    if reserva.profesor and reserva.profesor.perfil:
                        profesor_nombre = reserva.profesor.perfil.nombre
                    
                    # Determinar día de la semana
                    dia_semana = reserva.fecha_reserva.weekday()
                    dias_semana = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                    dia = dias_semana[dia_semana] if dia_semana < 5 else None
                    
                    if dia:
                        horario_completo.append({
                            'tipo_actividad': 'RESERVA',
                            'reserva': reserva,
                            'horaInicio': reserva.hora_inicio,
                            'horaFin': reserva.hora_fin,
                            'dia': dia,
                            'curso': None,
                            'profesor_nombre': profesor_nombre,
                            'motivo': "RESERVA",
                            'tipo': 'RES'
                        })
                        
                        # Agregar puntos de corte
                        puntos_corte.add(reserva.hora_inicio.replace(second=0, microsecond=0))
                        puntos_corte.add(reserva.hora_fin.replace(second=0, microsecond=0))
                
                print(f"DEBUG - Total actividades: {len(horario_completo)}")
                print(f"DEBUG - Puntos de corte: {len(puntos_corte)}")
                
                # ======================================================================
                # GENERAR HORAS DINÁMICAS
                # ======================================================================
                # Convertir puntos de corte de 'time' a 'datetime'
                dummy_date = hoy
                puntos_corte_dt = sorted(list(set([
                    datetime.combine(dummy_date, t) for t in puntos_corte
                ])))
                
                # Crear intervalos de hora
                horas = []
                hora_actual = None
                
                for dt_siguiente in puntos_corte_dt:
                    if hora_actual is None:
                        hora_actual = dt_siguiente
                        continue
                    
                    dt_inicio_fila = hora_actual
                    dt_fin_fila = dt_siguiente

                    duracion_minutos = (dt_fin_fila - dt_inicio_fila).total_seconds() / 60
                    if duracion_minutos < 11:
                        hora_actual = dt_siguiente
                        continue
                    
                    if dt_inicio_fila >= dt_fin_fila:
                        hora_actual = dt_siguiente
                        continue
                    
                    # Formatear rango de hora
                    hora_inicio_str = dt_inicio_fila.strftime("%H:%M")
                    hora_fin_str = dt_fin_fila.strftime("%H:%M")
                    horas.append(f"{hora_inicio_str} - {hora_fin_str}")
                    
                    hora_actual = dt_siguiente
                
                # Si no hay actividades, usar horas por defecto
                if not horas:
                    hora_actual = datetime.strptime('07:00', '%H:%M')
                    hora_final = datetime.strptime('21:00', '%H:%M')
                    
                    while hora_actual < hora_final:
                        hora_str = hora_actual.strftime('%H:%M')
                        hora_siguiente = hora_actual + timedelta(hours=1)
                        horas.append(f"{hora_str} - {hora_siguiente.strftime('%H:%M')}")
                        hora_actual = hora_siguiente
                
                print(f"DEBUG - Horas generadas: {horas}")
                
                # ======================================================================
                # ESTRUCTURAR DATOS DEL HORARIO
                # ======================================================================
                dias = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                horario_data = {}
                
                # Para cada intervalo de hora
                for hora_idx, rango_hora in enumerate(horas):
                    hora_inicio_str, hora_fin_str = rango_hora.split(" - ")
                    
                    # Convertir a datetime para comparar
                    try:
                        hora_inicio_dt = datetime.strptime(hora_inicio_str, "%H:%M").time()
                        hora_fin_dt = datetime.strptime(hora_fin_str, "%H:%M").time()
                    except:
                        # Si hay error, saltar este intervalo
                        continue
                    
                    horario_data[str(hora_idx)] = {}
                    
                    # Para cada día
                    for dia_idx, dia_key in enumerate(dias):
                        actividad_en_slot = None
                        
                        # Buscar actividad que coincida en este slot
                        for actividad in horario_completo:
                            # Verificar si es el día correcto
                            if actividad['dia'] != dia_key:
                                continue
                            
                            # Verificar si la actividad cubre este intervalo
                            actividad_inicio = actividad['horaInicio']
                            actividad_fin = actividad['horaFin']
                            
                            if actividad_inicio <= hora_inicio_dt and actividad_fin > hora_inicio_dt:
                                print(f"DEBUG - ¡COINCIDE! Actividad encontrada para {dia_key} {rango_hora}")
                                
                                # Es una clase regular
                                if actividad['tipo_actividad'] == 'CLASE':
                                    curso = actividad['curso']
                                    curso_id = str(curso.id)
                                    
                                    actividad_en_slot = {
                                        'tipo': actividad['tipo'],
                                        'nombre': curso.nombre,
                                        'codigo_curso': curso_id,
                                        'codigo_grupo': f"{curso_id}-{actividad['grupo']}",
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': curso_colores.get(curso_id, 'bg-secondary'),
                                        'hora_inicio': actividad_inicio.strftime("%H:%M:%S"),
                                        'hora_fin': actividad_fin.strftime("%H:%M:%S"),
                                        'es_reserva': False
                                    }
                                
                                # Es una reserva
                                else:
                                    actividad_en_slot = {
                                        'tipo': 'RES',
                                        'nombre': actividad.get('motivo', 'Reserva de Aula'),
                                        'codigo_curso': 'RES',
                                        'codigo_grupo': 'RESERVA',
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': 'bg-secondary',
                                        'hora_inicio': actividad_inicio.strftime("%H:%M:%S"),
                                        'hora_fin': actividad_fin.strftime("%H:%M:%S"),
                                        'es_reserva': True,
                                        'motivo': actividad.get('motivo', '')
                                    }
                                break
                        
                        horario_data[str(hora_idx)][str(dia_idx)] = actividad_en_slot
                        if actividad_en_slot:
                            print(f"DEBUG - Asignado: [{hora_idx}][{dia_idx}] = {actividad_en_slot['nombre']}")
                
                return JsonResponse({
                    'ok': True,
                    'aula': {
                        'id': aula.id,
                        'tipo': aula.tipo,
                    },
                    'horas': horas,
                    'horario_data': horario_data,
                    'curso_colores': curso_colores
                })
                
            except Aula.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Aula no encontrada'})
            except Exception as e:
                import traceback
                print(f"ERROR: {str(e)}")
                print(traceback.format_exc())
                return JsonResponse({'ok': False, 'msg': f'Error al cargar horarios: {str(e)}'})
        
        # Exportar horario a PDF
        elif accion == 'exportar_pdf':
            aula_id = request.GET.get('aula_id')
            
            try:
                aula = Aula.objects.get(id=aula_id)
                
                # USAR LA MISMA LÓGICA QUE EN LA VISTA PRINCIPAL
                from django.utils import timezone
                hoy = date.today()
                inicio_semana = hoy - timedelta(days=hoy.weekday())
                fin_semana = inicio_semana + timedelta(days=4)
                
                # 1. Obtener datos EXACTAMENTE como en la vista principal
                COLOR_OPTIONS = ['bg-primary', 'bg-success', 'bg-info', 'bg-warning', 'bg-danger', 'bg-secondary']
                curso_colores = {}
                color_index = 0
                
                horario_completo = []
                puntos_corte = set()
                
                # BloqueHorario para esta aula
                bloques_clases = BloqueHorario.objects.filter(aula=aula).select_related(
                    'grupo_curso__curso',
                    'grupo_curso__profesor__perfil'
                ).order_by('horaInicio')
                
                for bloque in bloques_clases:
                    tipo = "TEO"
                    try:
                        bloque.grupo_curso.grupoteoria
                    except:
                        tipo = "LAB"
                    
                    profesor_nombre = ""
                    if bloque.grupo_curso.profesor and bloque.grupo_curso.profesor.perfil:
                        profesor_nombre = bloque.grupo_curso.profesor.perfil.nombre
                    
                    horario_completo.append({
                        'tipo_actividad': 'CLASE',
                        'bloque': bloque,
                        'horaInicio': bloque.horaInicio,
                        'horaFin': bloque.horaFin,
                        'dia': bloque.dia,
                        'curso': bloque.grupo_curso.curso,
                        'profesor_nombre': profesor_nombre,
                        'grupo': bloque.grupo_curso.grupo,
                        'tipo': tipo
                    })
                    
                    puntos_corte.add(bloque.horaInicio.replace(second=0, microsecond=0))
                    puntos_corte.add(bloque.horaFin.replace(second=0, microsecond=0))
                    
                    curso_id = bloque.grupo_curso.curso.id
                    if curso_id not in curso_colores:
                        curso_colores[curso_id] = COLOR_OPTIONS[color_index % len(COLOR_OPTIONS)]
                        color_index += 1
                
                # Reservas
                reservas = Reserva.objects.filter(
                    aula=aula,
                    fecha_reserva__range=[inicio_semana, fin_semana]
                ).select_related('profesor__perfil')
                
                for reserva in reservas:
                    profesor_nombre = ""
                    if reserva.profesor and reserva.profesor.perfil:
                        profesor_nombre = reserva.profesor.perfil.nombre
                    
                    dia_semana = reserva.fecha_reserva.weekday()
                    dias_semana = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                    dia = dias_semana[dia_semana] if dia_semana < 5 else None
                    
                    if dia:
                        horario_completo.append({
                            'tipo_actividad': 'RESERVA',
                            'reserva': reserva,
                            'horaInicio': reserva.hora_inicio,
                            'horaFin': reserva.hora_fin,
                            'dia': dia,
                            'curso': None,
                            'profesor_nombre': profesor_nombre,
                            'motivo': 'RESERVA',
                            'tipo': 'RES'
                        })
                        
                        puntos_corte.add(reserva.hora_inicio.replace(second=0, microsecond=0))
                        puntos_corte.add(reserva.hora_fin.replace(second=0, microsecond=0))
                
                # 2. Generar horas dinámicas (MISMA LÓGICA)
                dummy_date = hoy
                puntos_corte_dt = sorted(list(set([
                    datetime.combine(dummy_date, t) for t in puntos_corte
                ])))
                
                horas = []
                hora_actual = None
                
                for dt_siguiente in puntos_corte_dt:
                    if hora_actual is None:
                        hora_actual = dt_siguiente
                        continue
                    
                    dt_inicio_fila = hora_actual
                    dt_fin_fila = dt_siguiente

                    duracion_minutos = (dt_fin_fila - dt_inicio_fila).total_seconds() / 60
                    if duracion_minutos < 11:
                        hora_actual = dt_siguiente
                        continue
                    
                    if dt_inicio_fila >= dt_fin_fila:
                        hora_actual = dt_siguiente
                        continue
                    
                    hora_inicio_str = dt_inicio_fila.strftime("%H:%M")
                    hora_fin_str = dt_fin_fila.strftime("%H:%M")
                    horas.append(f"{hora_inicio_str} - {hora_fin_str}")
                    
                    hora_actual = dt_siguiente
                
                if not horas:
                    hora_actual = datetime.strptime('07:00', '%H:%M')
                    hora_final = datetime.strptime('21:00', '%H:%M')
                    
                    while hora_actual < hora_final:
                        hora_str = hora_actual.strftime('%H:%M')
                        hora_siguiente = hora_actual + timedelta(hours=1)
                        horas.append(f"{hora_str} - {hora_siguiente.strftime('%H:%M')}")
                        hora_actual = hora_siguiente
                
                print(f"PDF DEBUG - Horas dinámicas: {len(horas)} intervalos")
                print(f"PDF DEBUG - Primeras horas: {horas[:3]}")
                
                # 3. Estructurar datos (MISMA LÓGICA)
                dias = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                dias_display = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes']
                horario_data = {}
                
                # Para cada intervalo de hora
                for hora_idx, rango_hora in enumerate(horas):
                    hora_inicio_str, hora_fin_str = rango_hora.split(" - ")
                    
                    try:
                        hora_inicio_dt = datetime.strptime(hora_inicio_str, "%H:%M").time()
                        hora_fin_dt = datetime.strptime(hora_fin_str, "%H:%M").time()
                    except:
                        continue
                    
                    horario_data[str(hora_idx)] = {}
                    
                    # Para cada día
                    for dia_idx, dia_key in enumerate(dias):
                        actividad_en_slot = None
                        
                        # Buscar actividad que coincida en este slot
                        for actividad in horario_completo:
                            # Verificar si es el día correcto
                            if actividad['dia'] != dia_key:
                                continue
                            
                            # Convertir horas de actividad
                            act_inicio = datetime.combine(dummy_date, actividad['horaInicio'])
                            act_fin = datetime.combine(dummy_date, actividad['horaFin'])
                            hora_inicio_dt_full = datetime.combine(dummy_date, hora_inicio_dt)
                            
                            # MISMA LÓGICA EXACTA: act_inicio <= hora_inicio_dt_full AND act_fin > hora_inicio_dt_full
                            if act_inicio <= hora_inicio_dt_full and act_fin > hora_inicio_dt_full:
                                
                                # Es una clase regular
                                if actividad['tipo_actividad'] == 'CLASE':
                                    curso = actividad['curso']
                                    curso_id = str(curso.id)
                                    
                                    actividad_en_slot = {
                                        'tipo': actividad['tipo'],
                                        'nombre': curso.nombre,
                                        'codigo_curso': curso_id,
                                        'codigo_grupo': f"{curso_id}-{actividad['grupo']}",
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': curso_colores.get(curso_id, 'bg-secondary'),
                                        'hora_inicio': actividad['horaInicio'].strftime("%H:%M"),
                                        'hora_fin': actividad['horaFin'].strftime("%H:%M"),
                                        'es_reserva': False
                                    }
                                
                                # Es una reserva
                                else:
                                    actividad_en_slot = {
                                        'tipo': 'RES',
                                        'nombre': actividad.get('motivo', 'Reserva de Aula'),
                                        'codigo_curso': 'RES',
                                        'codigo_grupo': 'RESERVA',
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': 'bg-secondary',
                                        'hora_inicio': actividad['horaInicio'].strftime("%H:%M"),
                                        'hora_fin': actividad['horaFin'].strftime("%H:%M"),
                                        'es_reserva': True,
                                        'motivo': actividad.get('motivo', '')
                                    }
                                break
                        
                        horario_data[str(hora_idx)][str(dia_idx)] = actividad_en_slot
                
                # DEBUG
                print(f"PDF DEBUG - Estructura final:")
                for hora_idx in range(len(horas)):
                    for dia_idx in range(len(dias)):
                        celda = horario_data[str(hora_idx)][str(dia_idx)]
                        if celda:
                            print(f"  [{hora_idx}][{dia_idx}]: {celda['nombre'][:20]}")
                
                curso_colores_map = {}
                for i, (curso_id, _) in enumerate(curso_colores.items()):
                    curso_colores_map[str(curso_id)] = f"color-{i % 10}"

                # Preparar contexto
                contexto = {
                    'aula': aula,
                    'horas': horas,
                    'horas_range': range(len(horas)),
                    'num_horas': len(horas),
                    'dias': dias_display,
                    'dias_range': range(len(dias)),
                    'horario_data': horario_data,
                    'curso_colores_map': curso_colores_map,
                    'fecha_emision': hoy.strftime("%d/%m/%Y"),
                    'semana_actual': f"{inicio_semana.strftime('%d/%m')} - {fin_semana.strftime('%d/%m')}"
                }
                
                # Renderizar template
                html_string = render_to_string('usuarios/secretaria/horario_aula_pdf.html', contexto)
                
                # Crear PDF
                response = HttpResponse(content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="horario_aula_{aula.id}_{hoy.strftime("%Y%m%d")}.pdf"'
                
                pisa_status = pisa.CreatePDF(
                    html_string, 
                    dest=response,
                    encoding='UTF-8'
                )
                
                if pisa_status.err:
                    return HttpResponse('Error al generar PDF', status=500)
                
                return response
                
            except Aula.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Aula no encontrada'})
            except Exception as e:
                import traceback
                print(f"ERROR PDF: {str(e)}")
                print(traceback.format_exc())
                return JsonResponse({'ok': False, 'msg': f'Error al generar PDF: {str(e)}'})

    # ======================================================================
    # 2. LÓGICA GET (RENDERIZADO DE LA PÁGINA INICIAL)
    # ======================================================================
    
    # Obtener todas las aulas ordenadas
    aulas = Aula.objects.all().order_by('id')
    
    # Definir horas del horario por defecto (para mostrar estructura inicial)
    horas = []
    hora_actual = datetime.strptime('07:00', '%H:%M')
    hora_final = datetime.strptime('21:00', '%H:%M')
    
    while hora_actual < hora_final:
        hora_str = hora_actual.strftime('%H:%M')
        hora_siguiente = hora_actual + timedelta(hours=1)
        horas.append(f"{hora_str} - {hora_siguiente.strftime('%H:%M')}")
        hora_actual = hora_siguiente
    
    # Días de la semana
    dias = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
    
    # Fecha actual para mostrar en la página
    from django.utils import timezone
    semana_actual = timezone.now()
    
    contexto = {
        'perfil': perfil_obj.perfil, 
        'titulo': 'Visualización de Horarios por Aula',
        'aulas': aulas,
        'horas': horas,
        'dias': dias,
        'curso_colores': {},
        'aula_seleccionada': None,
        'semana_actual': semana_actual
    }
    
    return render(request, 'usuarios/secretaria/ver_horarios_clases.html', contexto)

def gestion_laboratorios(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response: 
        return response

    # ======================================================================
    # 1. MANEJO DE SOLICITUDES AJAX (GET)
    # ======================================================================
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.method == 'GET':
        accion = request.GET.get('accion')
        
        # A. Obtener lista de cursos que tienen grupos de teoría (requisito para laboratorios)
        if accion == 'obtener_cursos_con_teoria':
            # Filtramos cursos que tienen al menos un grupo de teoría
            cursos_con_teoria = Curso.objects.filter(
                grupocurso__grupoteoria__isnull=False
            ).distinct().order_by('id')
            
            data = [{'id': c.id, 'nombre': c.nombre} for c in cursos_con_teoria]
            return JsonResponse({'cursos': data})
        
        # B. Obtener grupos de teoría de un curso específico
        if accion == 'obtener_grupos_teoria_curso':
            curso_id = request.GET.get('curso_id')
            # Solo grupos de teoría
            grupos = GrupoTeoria.objects.filter(grupo_curso__curso_id=curso_id).select_related('grupo_curso')
            data = [{'id': g.grupo_curso.id, 'grupo': g.grupo_curso.grupo} for g in grupos]
            return JsonResponse({'grupos_teoria': data})

        # C. Obtener lista de laboratorios de un curso específico
        if accion == 'obtener_laboratorios_curso':
            curso_id = request.GET.get('curso_id')
            # Filtramos solo grupos de laboratorio
            laboratorios = GrupoLaboratorio.objects.filter(
                grupo_curso__curso_id=curso_id
            ).select_related('grupo_curso')
            data = [{'id': g.grupo_curso.id, 'grupo': g.grupo_curso.grupo} for g in laboratorios]
            return JsonResponse({'laboratorios': data})

        # D. Obtener el detalle completo de un laboratorio específico
        if accion == 'obtener_detalle_laboratorio':
            laboratorio_id = request.GET.get('laboratorio_id')
            try:
                # Buscamos el GrupoCurso (que es la base)
                grupo_curso = GrupoCurso.objects.select_related('profesor__perfil').get(id=laboratorio_id)
                
                # Verificar que sea un laboratorio
                try:
                    grupo_lab = GrupoLaboratorio.objects.get(grupo_curso=grupo_curso)
                except GrupoLaboratorio.DoesNotExist:
                    return JsonResponse({'ok': False, 'msg': 'No es un laboratorio válido'})
                
                # Obtenemos sus bloques horarios
                bloques = BloqueHorario.objects.filter(grupo_curso=grupo_curso).select_related('aula')
                horarios = [{
                    'dia': b.dia,
                    'inicio': b.horaInicio.strftime('%H:%M:%S') if b.horaInicio else '',
                    'fin': b.horaFin.strftime('%H:%M:%S') if b.horaFin else '',
                    'aula_id': b.aula.id if b.aula else ''
                } for b in bloques]

                data = {
                    'id': grupo_curso.id,
                    'profesor_id': grupo_curso.profesor.perfil.id if grupo_curso.profesor else '',
                    'capacidad': grupo_curso.capacidad,
                    'horarios': horarios
                }
                return JsonResponse({'ok': True, 'data': data})
            except GrupoCurso.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Laboratorio no encontrado'})
            except Exception as e:
                return JsonResponse({'ok': False, 'msg': f'Error al obtener detalle: {str(e)}'})

        return JsonResponse({'ok': False, 'msg': 'Acción desconocida'})

    # ======================================================================
    # 2. MANEJO DE SOLICITUDES POST (CRUD)
    # ======================================================================
    if request.method == 'POST':
        accion = request.POST.get('accion')
        
        try:
            with transaction.atomic():
                
                # --- A. CREAR LABORATORIO ---
                if accion == 'crear_laboratorio':
                    curso_id = request.POST.get('curso_id')
                    grupo_teoria_id = request.POST.get('grupo_teoria_id')  # Grupo teoría asociado
                    letra_grupo = request.POST.get('grupo', '').upper()  # Letra para el laboratorio
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    print(f"DEBUG - horarios_json recibido: {horarios_json}")
                    
                    # Verificar que el grupo de teoría exista
                    try:
                        grupo_teoria = GrupoTeoria.objects.get(grupo_curso_id=grupo_teoria_id)
                    except GrupoTeoria.DoesNotExist:
                        messages.error(request, f"El grupo de teoría {grupo_teoria_id} no existe.")
                        return redirect('usuarios:gestion_laboratorios')
                    
                    # Generación del ID Compuesto: "L" + CURSOID + LETRA
                    nuevo_id = f"L{curso_id}{letra_grupo}"
                    
                    if GrupoCurso.objects.filter(id=nuevo_id).exists():
                        messages.error(request, f"El Laboratorio {letra_grupo} para este curso ya existe (ID: {nuevo_id}).")
                    else:
                        try:
                            # Validar horarios_json
                            if not horarios_json or horarios_json.strip() == '':
                                raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                            
                            # 1. Parsear JSON
                            horarios = json.loads(horarios_json)
                            
                            # Validar que haya al menos un horario
                            if not isinstance(horarios, list) or len(horarios) == 0:
                                raise ValueError("Debe agregar al menos un bloque de horario.")
                            
                            # 2. Iniciar Transacción Atómica
                            with transaction.atomic():
                                # Validación de existencia de objetos FK y conversión de capacidad
                                profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                                curso = Curso.objects.get(id=curso_id)
                                capacidad_int = int(capacidad)
                                
                                # Verificar que el profesor sea de laboratorio
                                if profesor and not profesor.es_lab:
                                    raise ValueError(f"El profesor {profesor.perfil.nombre} no está habilitado para laboratorios.")
                                
                                # 3. Validar cada horario
                                for i, h in enumerate(horarios, 1):
                                    try:
                                        aula_obj = Aula.objects.get(id=h['aula_id'])
                                    except ObjectDoesNotExist:
                                        raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                    
                                    # Validar que el aula sea de tipo laboratorio
                                    if aula_obj.tipo != 'LABORATORIO':
                                        raise ValueError(f"El aula {aula_obj.id} no es un laboratorio. Solo se pueden usar aulas tipo LABORATORIO.")
                                    
                                    # Validar campos requeridos
                                    if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                        raise ValueError(f"El horario {i} tiene campos incompletos.")
                                    
                                    # Convertir tiempos
                                    try:
                                        inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                        fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                    except ValueError:
                                        raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                    
                                    # Validar que inicio < fin
                                    if inicio_time >= fin_time:
                                        raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                    
                                    # 3.1 Verificar cruce con horarios existentes en la misma aula
                                    horarios_cruzados_aula = BloqueHorario.objects.filter(
                                        aula=aula_obj,
                                        dia=h['dia']
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_cruzados_aula.exists():
                                        conflicto = horarios_cruzados_aula.first()
                                        curso_conflicto = conflicto.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto.grupo_curso.grupo
                                        raise ValueError(
                                            f"Conflicto de horario en laboratorio {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                            f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                        )
                                    
                                    # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                    if profesor:
                                        # Buscar horarios donde el docente ya tenga clases asignadas
                                        horarios_docente = BloqueHorario.objects.filter(
                                            grupo_curso__profesor=profesor,
                                            dia=h['dia']
                                        ).exclude(
                                            Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                        )
                                        
                                        if horarios_docente.exists():
                                            conflicto_docente = horarios_docente.first()
                                            curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                            grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                            aula_conflicto = conflicto_docente.aula.id
                                            raise ValueError(
                                                f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                                f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                            )

                                # 4. Crear el GrupoCurso base
                                nuevo_grupo = GrupoCurso.objects.create(
                                    id=nuevo_id,
                                    curso=curso,
                                    profesor=profesor,
                                    grupo=letra_grupo,
                                    capacidad=capacidad_int
                                )
                                
                                # 5. Crear la especialización GrupoLaboratorio
                                GrupoLaboratorio.objects.create(grupo_curso=nuevo_grupo)
                                
                                # 6. Crear los Bloques de Horario
                                for h in horarios:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                    BloqueHorario.objects.create(
                                        dia=h['dia'],
                                        horaInicio=h['inicio'],
                                        horaFin=h['fin'],
                                        grupo_curso=nuevo_grupo,
                                        aula=aula_obj
                                    )
                            
                            messages.success(request, f"Laboratorio {letra_grupo} creado exitosamente.")

                        except ObjectDoesNotExist as e:
                            messages.error(request, f"Error de referencia: {str(e)}")
                        except ValueError as e:
                            messages.error(request, f"Error en los datos: {str(e)}")
                        except json.JSONDecodeError as e:
                            messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                        except Exception as e:
                            messages.error(request, f"Error inesperado al crear el laboratorio: {str(e)}")

                # --- B. EDITAR LABORATORIO ---
                elif accion == 'editar_laboratorio':
                    laboratorio_id = request.POST.get('laboratorio_id')  # ID completo (Ej: INF101AL)
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    # 1. Recuperar GrupoCurso o lanzar 404 si no existe
                    grupo_curso = get_object_or_404(GrupoCurso, id=laboratorio_id)
                    
                    # Verificar que sea un laboratorio
                    try:
                        grupo_lab = GrupoLaboratorio.objects.get(grupo_curso=grupo_curso)
                    except GrupoLaboratorio.DoesNotExist:
                        messages.error(request, "El grupo seleccionado no es un laboratorio.")
                        return redirect('usuarios:gestion_laboratorios')
                    
                    try:
                        # Validar horarios_json
                        if not horarios_json or horarios_json.strip() == '':
                            raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                        
                        # Parsear JSON
                        horarios = json.loads(horarios_json)
                        
                        # Validar que haya al menos un horario
                        if not isinstance(horarios, list) or len(horarios) == 0:
                            raise ValueError("Debe agregar al menos un bloque de horario.")
                        
                        # 2. Iniciar Transacción Atómica
                        with transaction.atomic():
                            # Validación de existencia de objetos FK y conversión de capacidad
                            profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                            capacidad_int = int(capacidad)
                            
                            # Verificar que el profesor sea de laboratorio
                            if profesor and not profesor.es_lab:
                                raise ValueError(f"El profesor {profesor.perfil.nombre} no está habilitado para laboratorios.")
                            
                            # 3. Validar cada horario antes de eliminar los existentes
                            for i, h in enumerate(horarios, 1):
                                try:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                except ObjectDoesNotExist:
                                    raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                
                                # Validar que el aula sea de tipo laboratorio
                                if aula_obj.tipo != 'LABORATORIO':
                                    raise ValueError(f"El aula {aula_obj.id} no es un laboratorio. Solo se pueden usar aulas tipo LABORATORIO.")
                                
                                # Validar campos requeridos
                                if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                    raise ValueError(f"El horario {i} tiene campos incompletos.")
                                
                                # Convertir tiempos
                                try:
                                    inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                    fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                except ValueError:
                                    raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                
                                # Validar que inicio < fin
                                if inicio_time >= fin_time:
                                    raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                
                                # 3.1 Verificar cruce con horarios existentes en la misma aula
                                # Excluir los horarios del laboratorio actual que estamos editando
                                horarios_cruzados_aula = BloqueHorario.objects.filter(
                                    aula=aula_obj,
                                    dia=h['dia']
                                ).exclude(
                                    grupo_curso=grupo_curso  # Excluir los horarios del laboratorio actual
                                ).exclude(
                                    Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                )
                                
                                if horarios_cruzados_aula.exists():
                                    conflicto = horarios_cruzados_aula.first()
                                    curso_conflicto = conflicto.grupo_curso.curso.nombre
                                    grupo_conflicto = conflicto.grupo_curso.grupo
                                    raise ValueError(
                                        f"Conflicto de horario en laboratorio {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                        f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                    )
                                
                                # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                if profesor:
                                    # Buscar horarios donde el docente ya tenga clases asignadas
                                    # Excluir los horarios del laboratorio actual que estamos editando
                                    horarios_docente = BloqueHorario.objects.filter(
                                        grupo_curso__profesor=profesor,
                                        dia=h['dia']
                                    ).exclude(
                                        grupo_curso=grupo_curso  # Excluir los horarios del laboratorio actual
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_docente.exists():
                                        conflicto_docente = horarios_docente.first()
                                        curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                        aula_conflicto = conflicto_docente.aula.id
                                        raise ValueError(
                                            f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                            f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                        )

                            # 4. Actualizar datos básicos
                            grupo_curso.profesor = profesor
                            grupo_curso.capacidad = capacidad_int
                            grupo_curso.save()
                            
                            # 5. Actualizar horarios: Estrategia de reemplazo total (Borrar y crear)
                            BloqueHorario.objects.filter(grupo_curso=grupo_curso).delete()
                            
                            for h in horarios:
                                aula_obj = Aula.objects.get(id=h['aula_id'])
                                BloqueHorario.objects.create(
                                    dia=h['dia'],
                                    horaInicio=h['inicio'],
                                    horaFin=h['fin'],
                                    grupo_curso=grupo_curso,
                                    aula=aula_obj
                                )
                        
                        messages.success(request, "Laboratorio actualizado correctamente.")
                    
                    except ObjectDoesNotExist as e:
                        messages.error(request, f"Error de referencia: {str(e)}")
                    except ValueError as e:
                        messages.error(request, f"Error en los datos proporcionados: {str(e)}")
                    except json.JSONDecodeError as e:
                        messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                    except Exception as e:
                        messages.error(request, f"Error inesperado al actualizar el laboratorio: {str(e)}")

                # --- C. ELIMINAR LABORATORIO ---
                elif accion == 'eliminar_laboratorio':
                    laboratorio_id = request.POST.get('laboratorio_id')
                    # Verificar que sea un laboratorio
                    try:
                        grupo_lab = GrupoLaboratorio.objects.get(grupo_curso_id=laboratorio_id)
                        # Al eliminar GrupoCurso, el CASCADE elimina GrupoLaboratorio y BloqueHorario
                        GrupoCurso.objects.filter(id=laboratorio_id).delete()
                        messages.success(request, "Laboratorio eliminado correctamente.")
                    except GrupoLaboratorio.DoesNotExist:
                        messages.error(request, "El grupo seleccionado no es un laboratorio.")

        except IntegrityError as e:
            messages.error(request, f"Error de integridad en la base de datos: {str(e)}")
        except Exception as e:
            messages.error(request, f"Ocurrió un error inesperado: {str(e)}")
            
        return redirect('usuarios:gestion_laboratorios')

    # ======================================================================
    # 3. LÓGICA GET (RENDERIZADO DE LA PÁGINA)
    # ======================================================================
    
    # Obtener cursos con laboratorios y sus conteos
    cursos = Curso.objects.filter(
        grupocurso__grupolaboratorio__isnull=False
    ).distinct().annotate(
        num_laboratorios=Count('grupocurso__grupolaboratorio', distinct=True),
        num_matriculados=Count('grupocurso__grupolaboratorio__matriculalaboratorio', 
                               filter=Q(grupocurso__grupolaboratorio__isnull=False), 
                               distinct=True)
    ).order_by('id')

    # Listas auxiliares
    profesores_lab = Profesor.objects.filter(es_lab=True).select_related('perfil').order_by('perfil__nombre')
    aulas_lab = Aula.objects.filter(tipo='LABORATORIO').order_by('id')

    contexto = {
        'perfil': perfil_obj.perfil, 
        'titulo': 'Gestión de Laboratorios',
        'cursos': cursos,
        'profesores': profesores_lab,
        'aulas': aulas_lab
    }
    return render(request, 'usuarios/secretaria/gestion_laboratorios.html', contexto)

def registro_estudiantes(request):
    # ===========================
    #   SUBIR CSV
    # ===========================
    if request.method == "POST" and "subir_csv" in request.POST:
        try:
            file = request.FILES.get("csv_estudiantes")

            if not file:
                messages.error(request, "Debe seleccionar un archivo CSV.")
                return redirect("usuarios:registro_estudiantes")

            # Convertimos a texto UTF-8
            decoded = file.read().decode("utf-8")
            reader = csv.reader(decoded.splitlines())

            # Formato esperado (sin encabezados):
            # cui, nombre, email, password
            for row in reader:
                if len(row) < 4:
                    continue

                cui = row[0].strip()
                nombre = row[1].strip()
                email = row[2].strip() if row[2].strip() else None
                password = row[3].strip()

                if not (cui and nombre and password):
                    continue  # Campos obligatorios

                # Crear o actualizar Perfil
                perfil, created = Perfil.objects.get_or_create(
                    id=cui,
                    defaults={
                        "nombre": nombre,
                        "email": email,
                        "password": password,
                        "rol": "ESTUDIANTE",
                        "estadoCuenta": True
                    }
                )

                # Si el perfil ya existe, actualizar nombre/email/password si cambian
                if not created:
                    perfil.nombre = nombre
                    perfil.email = email
                    perfil.password = password
                    perfil.rol = "ESTUDIANTE"
                    perfil.estadoCuenta = True
                    perfil.save()

                # Crear estudiante si no existe
                Estudiante.objects.get_or_create(perfil=perfil)

            messages.success(request, "CSV procesado correctamente.")

        except Exception as e:
            messages.error(request, f"Error procesando CSV: {str(e)}")

        return redirect("usuarios:registro_estudiantes")

    # ===========================
    #   CREAR ESTUDIANTE MANUAL
    # ===========================
    if request.method == "POST" and "crear_estudiante" in request.POST:
        cui = request.POST.get("cui")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        password = request.POST.get("password")

        # Validación SOLO de los campos reales
        if not (cui and nombre and password):
            messages.error(request, "CUI, Nombre y Contraseña son obligatorios.")
            return redirect("usuarios:registro_estudiantes")

        # Crear Perfil
        perfil = Perfil.objects.create(
            id=cui,
            nombre=nombre,
            email=email if email else None,
            password=password,
            rol="ESTUDIANTE",
            estadoCuenta=True
        )

        # Crear Estudiante
        Estudiante.objects.create(perfil=perfil)

        messages.success(request, "Estudiante creado correctamente.")
        return redirect("usuarios:registro_estudiantes")

    # ===========================
    #   EDITAR ESTUDIANTE
    # ===========================
    if request.method == "POST" and "editar_estudiante" in request.POST:
        est_id = request.POST.get("estudiante_id")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        estado = request.POST.get("estadoCuenta")
        grupo_id = request.POST.get("grupo_curso_id")

        perfil = Perfil.objects.get(id=est_id)

        # Actualizar datos del perfil
        perfil.nombre = nombre
        perfil.email = email if email else None
        perfil.estadoCuenta = (estado == "True")
        perfil.save()

        # Asignar curso si corresponde
        if grupo_id:
            estudiante = Estudiante.objects.get(perfil_id=est_id)
            grupo = GrupoCurso.objects.get(id=grupo_id)

            Matricula.objects.get_or_create(
                estudiante=estudiante,
                grupo_curso=grupo,
                defaults={"estado": True}
            )

        messages.success(request, "Estudiante actualizado.")
        return redirect("usuarios:registro_estudiantes")
    
    # =============================
    #   ELIMINAR ESTUDIANTE
    # =============================
    if request.method == "POST" and "eliminar_estudiante" in request.POST:
        estudiante_id = request.POST.get("estudiante_id")

        try:
            perfil = Perfil.objects.get(id=estudiante_id)

            # Al eliminar el perfil, se elimina automáticamente el Estudiante
            # por el OnDelete.CASCADE
            perfil.delete()

            messages.success(request, "La cuenta del estudiante fue eliminada correctamente.")
        except Perfil.DoesNotExist:
            messages.error(request, "El estudiante no existe.")

        return redirect("usuarios:registro_estudiantes")

    # ===========================
    #     MATRICULAR ESTUDIANTE
    # ===========================
    if request.method == "POST" and "asignar_curso" in request.POST:

        estudiante_id = request.POST.get("estudiante_id")
        grupo_curso_id = request.POST.get("grupo_curso_id")

        try:
            estudiante = Estudiante.objects.get(pk=estudiante_id)
            grupo_curso = GrupoCurso.objects.get(pk=grupo_curso_id)

            # Crear la matrícula
            Matricula.objects.create(
                estudiante=estudiante,
                grupo_curso=grupo_curso,
                estado=True  # Activo por defecto
            )

            messages.success(request, "Curso asignado correctamente.")

        except Estudiante.DoesNotExist:
            messages.error(request, "El estudiante no existe.")

        except GrupoCurso.DoesNotExist:
            messages.error(request, "El grupo del curso no existe.")

        except:
            messages.error(request, "Este estudiante ya está matriculado en ese grupo.")

        return redirect("usuarios:registro_estudiantes")

    # Activar / Desactivar cuenta
    if "toggle_estado" in request.POST:
        perfil = Perfil.objects.get(id=request.POST["estudiante_id"])
        perfil.estadoCuenta = not perfil.estadoCuenta
        perfil.save()
        messages.success(request, "Estado actualizado.")
        return redirect("usuarios:registro_estudiantes")

    # ===========================
    #   LISTAR ESTUDIANTES
    # ===========================
    estudiantes = Estudiante.objects.select_related("perfil").all()
    grupos = GrupoCurso.objects.filter(grupoteoria__isnull=False).select_related('curso', 'profesor__perfil').all().order_by("curso__nombre")

    context = {
        "estudiantes": estudiantes,
        "grupos": grupos,
    }
    return render(request, "usuarios/secretaria/registro_estudiantes.html", context)

def detalle_estudiante(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response:
        return response

    cui = request.GET.get('cui') or (
        request.resolver_match.kwargs.get('cui')
        if request.resolver_match else None
    )
    if not cui:
        messages.error(request, "No se indicó CUI del estudiante.")
        return redirect(reverse('usuarios:registro_estudiantes'))

    # Obtener estudiante
    perfil_estudiante = get_object_or_404(Perfil, pk=cui)
    estudiante = get_object_or_404(Estudiante, perfil=perfil_estudiante)

    # Matriculas
    matriculas_qs = Matricula.objects.filter(
        estudiante=estudiante
    ).select_related(
        'grupo_curso__curso',
        'grupo_curso__profesor__perfil'
    ).order_by('grupo_curso__curso__nombre')

    # Laboratorios inscritos
    labs_qs = (
        MatriculaLaboratorio.objects.filter(estudiante=estudiante)
        .select_related(
            "laboratorio__grupo_curso__curso",
            "laboratorio__grupo_curso__profesor__perfil"
        )
    )

    laboratorios = []
    for mlab in labs_qs:
        glab = mlab.laboratorio          # GrupoLaboratorio
        gcurso = glab.grupo_curso        # GrupoCurso asociado

        laboratorios.append({
            "id": glab.pk,
            "curso": gcurso.curso.nombre,
            "curso_id": gcurso.curso.id,
            "profesor": gcurso.profesor.perfil.nombre if gcurso.profesor else "-",
            "grupo": gcurso.grupo,
        })

    matriculas = []
    grupo_ids = []

    for m in matriculas_qs:
        matriculas.append({
            'id': m.id,
            'grupo': m.grupo_curso,
            'estado': m.estado,
        })
        grupo_ids.append(m.grupo_curso.id)

    # Horarios
    horarios_map = {}
    if grupo_ids:
        bloques = BloqueHorario.objects.filter(
            grupo_curso__id__in=grupo_ids
        ).select_related('aula', 'grupo_curso')

        for b in bloques:
            gid = b.grupo_curso.id
            horarios_map.setdefault(gid, []).append({
                'dia': b.dia,
                'horaInicio': b.horaInicio.strftime("%H:%M"),
                'horaFin': b.horaFin.strftime("%H:%M"),
                'aula': getattr(b.aula, 'nombre', getattr(b.aula, 'id', '-'))
            })

    # Cargar NOTAS desde la tabla Matricula
    notas_por_curso = {}
    for m in matriculas_qs:
        gid = m.grupo_curso.id
        notas_por_curso[gid] = {
            'EC1': m.EC1,
            'EP1': m.EP1,
            'EC2': m.EC2,
            'EP2': m.EP2,
            'EC3': m.EC3,
            'EP3': m.EP3,
        }

    # Asistencias
    asistencias_qs = RegistroAsistenciaDetalle.objects.filter(
        estudiante=estudiante
    ).select_related(
        'registro_asistencia__grupo_curso__curso'
    ).order_by('-registro_asistencia__fechaClase')[:30]

    asistencias = []
    total_presentes = 0
    total_faltas = 0

    for d in asistencias_qs:
        fecha = d.registro_asistencia.fechaClase.strftime("%Y-%m-%d")
        grupo = d.registro_asistencia.grupo_curso
        curso_nombre = grupo.curso.nombre if grupo and grupo.curso else ''
        estado = d.estado

        if estado == 'PRESENTE':
            total_presentes += 1
        else:
            total_faltas += 1

        asistencias.append({
            'fecha': fecha,
            'grupo_id': grupo.id if grupo else '',
            'curso_nombre': curso_nombre,
            'estado': estado
        })

    resumen = {
        'total_matriculas': matriculas_qs.count(),
        'total_presentes': total_presentes,
        'total_faltas': total_faltas
    }

    contexto = {
        'perfil': perfil_obj,
        'titulo': f'Perfil Estudiante - {perfil_estudiante.nombre}',
        'perfil_estudiante': perfil_estudiante,
        'estudiante': estudiante,
        'matriculas': [{'grupo': m['grupo'], 'estado': m['estado']} for m in matriculas],
        'horarios_by_group': {'mat_map': horarios_map},
        'notas_por_curso': notas_por_curso,
        'laboratorios': laboratorios,
        'asistencias': asistencias,
        'resumen': resumen,
    }

    return render(request, 'usuarios/secretaria/detalle_estudiante.html', contexto)

def detalle_profesor(request):
    perfil_obj, response = check_secretaria_auth(request)
    if response:
        return response

    codigo = request.GET.get("codigo") or (
        request.resolver_match.kwargs.get("codigo")
        if request.resolver_match else None
    )
    if not codigo:
        messages.error(request, "No se indicó Código del profesor.")
        return redirect(reverse("usuarios:registro_profesores"))

    # ================================
    #   1. Obtener profesor
    # ================================
    perfil_prof = get_object_or_404(Perfil, pk=codigo)
    profesor = get_object_or_404(Profesor, perfil=perfil_prof)

    # ================================
    #   2. Obtener sus grupos asignados
    # ================================
    grupos_qs = (
        GrupoCurso.objects.filter(profesor=profesor)
        .select_related("curso", "profesor__perfil")
        .order_by("curso__nombre")
    )

    # Determinar qué grupos son teoría
    grupos_teoria_ids = set(
        GrupoTeoria.objects.filter(grupo_curso__in=grupos_qs)
        .values_list("grupo_curso_id", flat=True)
    )

    # ================================
    #   3. Horarios por cada grupo
    # ================================
    horarios_map = {}
    if grupos_qs:
        bloques = (
            BloqueHorario.objects.filter(grupo_curso__in=grupos_qs)
            .select_related("aula", "grupo_curso")
        )
        for b in bloques:
            gid = b.grupo_curso.id
            horarios_map.setdefault(gid, []).append({
                "dia": b.dia,
                "horaInicio": b.horaInicio.strftime("%H:%M"),
                "horaFin": b.horaFin.strftime("%H:%M"),
                "aula": getattr(b.aula, "nombre", getattr(b.aula, "id", "-")),
            })

    # ================================
    #   4. Temas (solo para teoría)
    # ================================
    temas_map = {}
    teoria_ids = list(grupos_teoria_ids)

    if teoria_ids:
        temas_qs = (
            TemaCurso.objects.filter(grupo_teoria_id__in=teoria_ids)
            .order_by("orden")
        )
        for t in temas_qs:
            temas_map.setdefault(t.grupo_teoria_id, []).append({
                "orden": t.orden,
                "nombre": t.nombre,
                "fecha": t.fecha.strftime("%Y-%m-%d") if t.fecha else "-",
                "completado": t.completado,
            })

    # ================================
    #   5. Reservas del profesor
    # ================================
    reservas_qs = (
        Reserva.objects
        .filter(profesor=profesor)
        .select_related("aula")
        .order_by("-fecha_reserva")
    )

    reservas = []

    for r in reservas_qs:
        reservas.append({
            "fecha_reserva": r.fecha_reserva.strftime("%Y-%m-%d"),
            "hora_inicio": r.hora_inicio.strftime("%H:%M"),
            "hora_fin": r.hora_fin.strftime("%H:%M"),
            "aula_id": r.aula.id,
            "aula_tipo": r.aula.get_tipo_display(),
        })

    # ================================
    #   6. Asistencias tomadas
    # ================================
    grupos_ids = GrupoCurso.objects.filter(profesor=profesor).values_list('id', flat=True)

    asistencias_qs = RegistroAsistencia.objects.filter(
        grupo_curso__id__in=grupos_ids
    ).select_related('grupo_curso__curso').order_by('-fechaClase')[:30]

    asistencias = [{
        'fechaClase': r.fechaClase.strftime("%Y-%m-%d"),
        'horaInicioVentana': r.horaInicioVentana.strftime("%H:%M"),
        'grupo_curso': r.grupo_curso,
        'ipProfesor': r.ipProfesor
    } for r in asistencias_qs]

    # ================================
    #   7. Resumen
    # ================================
    resumen = {
        "total_cursos": grupos_qs.count(),
        "total_reservas": reservas_qs.count(),
        "total_asistencias": asistencias_qs.count(),
    }

    # ================================
    #   8. Contexto final
    # ================================
    contexto = {
        "perfil": perfil_obj,
        "profesor": profesor,
        "grupos": grupos_qs,
        "grupos_teoria_ids": grupos_teoria_ids,
        "horarios": horarios_map,
        "temas": temas_map,
        "reservas": reservas,
        "asistencias": asistencias,
        "resumen": resumen,
        "titulo": f"Perfil Profesor - {profesor.perfil.nombre}",
    }

    return render(request, "usuarios/secretaria/detalle_profesor.html", contexto)

def registro_profesores(request):
    # ===========================
    #   SUBIR CSV
    # ===========================
    if request.method == "POST" and "subir_csv" in request.POST:
        try:
            file = request.FILES.get("csv_profesores")

            if not file:
                messages.error(request, "Debe seleccionar un archivo CSV.")
                return redirect("usuarios:registro_profesores")

            decoded = file.read().decode("utf-8")
            reader = csv.reader(decoded.splitlines())

            # Formato esperado:
            # cui, nombre, email, password
            for row in reader:
                if len(row) < 4:
                    continue

                codigo = row[0].strip()
                nombre = row[1].strip()
                email = row[2].strip() or None
                password = row[3].strip()

                if not (codigo and nombre and password):
                    continue

                perfil, created = Perfil.objects.get_or_create(
                    id=codigo,
                    defaults={
                        "nombre": nombre,
                        "email": email,
                        "password": password,
                        "rol": "PROFESOR",
                        "estadoCuenta": True
                    }
                )

                if not created:
                    perfil.nombre = nombre
                    perfil.email = email
                    perfil.password = password
                    perfil.rol = "PROFESOR"
                    perfil.estadoCuenta = True
                    perfil.save()

                Profesor.objects.get_or_create(perfil=perfil)

            messages.success(request, "CSV procesado correctamente.")

        except Exception as e:
            messages.error(request, f"Error procesando CSV: {str(e)}")

        return redirect("usuarios:registro_profesores")

    # ===========================
    #   CREAR PROFESOR MANUAL
    # ===========================
    if request.method == "POST" and "crear_profesor" in request.POST:

        codigo = request.POST.get("cui")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        password = request.POST.get("password")

        if not (codigo and nombre and password):
            messages.error(request, "Código, Nombre y Contraseña son obligatorios.")
            return redirect("usuarios:registro_profesores")

        perfil = Perfil.objects.create(
            id=codigo,
            nombre=nombre,
            email=email or None,
            password=password,
            rol="PROFESOR",
            estadoCuenta=True
        )

        Profesor.objects.create(perfil=perfil)

        messages.success(request, "Profesor creado correctamente.")
        return redirect("usuarios:registro_profesores")

    # ===========================
    #   EDITAR PROFESOR
    # ===========================
    if request.method == "POST" and "editar_profesor" in request.POST:
        profesor_id = request.POST.get("profesor_id")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        estado = request.POST.get("estadoCuenta")
        asignar_grupo_id = request.POST.get("grupo_curso_id")
        tipo_clase = request.POST.get("tipo_clase")

        profe = Profesor.objects.get(perfil__id=profesor_id)
        if tipo_clase == "ambos":
            profe.es_teoria = True
            profe.es_lab = True
        elif tipo_clase == "teoria":
            profe.es_teoria = True
            profe.es_lab = False
        elif tipo_clase == "laboratorio":
            profe.es_teoria = False
            profe.es_lab = True
        
        profe.save()

        perfil = Perfil.objects.get(id=profesor_id)

        perfil.nombre = nombre
        perfil.email = email or None
        perfil.estadoCuenta = (estado == "True")
        perfil.save()

        if asignar_grupo_id:
            grupo = GrupoCurso.objects.get(id=asignar_grupo_id)
            grupo.profesor = profe
            grupo.save()

        messages.success(request, "Profesor actualizado correctamente.")
        return redirect("usuarios:registro_profesores")

    # ===========================
    #   ELIMINAR PROFESOR
    # ===========================
    if request.method == "POST" and "eliminar_profesor" in request.POST:

        profesor_id = request.POST.get("profesor_id")

        try:
            perfil = Perfil.objects.get(id=profesor_id)
            perfil.delete()
            messages.success(request, "La cuenta del profesor fue eliminada.")
        except Perfil.DoesNotExist:
            messages.error(request, "El profesor no existe.")

        return redirect("usuarios:registro_profesores")

    # ===========================
    #   ACTIVAR / DESACTIVAR
    # ===========================
    if "toggle_estado" in request.POST:

        perfil = Perfil.objects.get(id=request.POST["profesor_id"])
        perfil.estadoCuenta = not perfil.estadoCuenta
        perfil.save()
        messages.success(request, "Estado actualizado.")
        return redirect("usuarios:registro_profesores")

    # ===========================
    #   LISTAR PROFESORES
    # ===========================
    profesores = Profesor.objects.select_related("perfil").all().order_by("perfil__nombre")
    grupos_disponibles = GrupoCurso.objects.filter(profesor__isnull=True).filter(Q(grupoteoria__isnull=False) | Q(grupolaboratorio__isnull=False)).select_related("curso").order_by("curso__nombre", "grupo")

    return render(request, "usuarios/secretaria/registro_profesores.html", {
        "profesores": profesores,   
        "grupos_disponibles_profesor": grupos_disponibles,
    })

# ----------------------------------------------------------------------
# 4. VISTAS DEL ADMINISTRADOR
# ----------------------------------------------------------------------

def check_administrador_auth(request):
    """Función de ayuda para verificar la sesión y el rol de Admin."""
    if not request.session.get('is_authenticated') or request.session.get('usuario_rol') != 'ADMIN':
        messages.warning(request, "Acceso denegado o rol incorrecto.")
        return None, redirect('usuarios:selector_rol')
    
    usuario_id = request.session['usuario_id']
    try:
        admin_obj = Administrador.objects.select_related('perfil').get(perfil_id=usuario_id)
        return admin_obj, None
    except Administrador.DoesNotExist:
        messages.error(request, "Error: Datos de secretaria no encontrados.")
        return None, redirect('usuarios:logout')
    
def dashboard_admin(request):
    """Muestra la página de inicio del dashboard de Administrador."""
    admin_obj, response = check_administrador_auth(request)
    if response:
        return response
    
    from django.db.models import Count, Q, Sum, Avg
    from django.utils import timezone
    from datetime import date, timedelta
    
    # =============================================
    # 1. CONTEOS BÁSICOS
    # =============================================
    conteo_profesores = Profesor.objects.count()
    conteo_estudiantes = Estudiante.objects.count()
    conteo_secretarias = Secretaria.objects.count()
    conteo_cursos = Curso.objects.count()
    conteo_aulas = Aula.objects.count()
    
    # =============================================
    # 2. ESTADÍSTICAS DE MATRÍCULAS
    # =============================================
    # Total de matrículas activas (estado=True)
    total_matriculas = Matricula.objects.filter(estado=True).count()
    
    # Matrículas por tipo (Teoría vs Laboratorio)
    matriculas_teoria = Matricula.objects.filter(
        estado=True,
        grupo_curso__grupoteoria__isnull=False
    ).count()
    
    matriculas_lab = MatriculaLaboratorio.objects.count()
    
    # =============================================
    # 3. ESTADÍSTICAS DE HORARIOS
    # =============================================
    # Total de bloques de horario asignados
    total_bloques = BloqueHorario.objects.count()
    
    # Bloques por tipo de aula
    bloques_aula_normal = BloqueHorario.objects.filter(
        aula__tipo='AULA_NORMAL'
    ).count()
    
    bloques_laboratorio = BloqueHorario.objects.filter(
        aula__tipo='LABORATORIO'
    ).count()
    
    # =============================================
    # 4. ESTADÍSTICAS DE RESERVAS (semana actual: Lunes a Viernes)
    # =============================================
    hoy = date.today()
    
    # Calcular el lunes de la semana actual (0=Lunes, 6=Domingo)
    dias_hasta_lunes = hoy.weekday()  # 0 para lunes, 1 para martes, etc.
    lunes_semana_actual = hoy - timedelta(days=dias_hasta_lunes)
    
    # Calcular el viernes de esta semana
    dias_hasta_viernes = 4 - hoy.weekday()  # 0 si es viernes, positivo si es antes
    if dias_hasta_viernes < 0:
        # Si ya pasó el viernes, ir al viernes de la siguiente semana
        dias_hasta_viernes += 7
    viernes_semana_actual = hoy + timedelta(days=dias_hasta_viernes)
    
    # Para estadísticas de "Actividad Reciente", usamos desde el lunes de esta semana
    # hasta el viernes de esta semana
    fecha_inicio_semana = lunes_semana_actual
    fecha_fin_semana = viernes_semana_actual
    
    reservas_semana = Reserva.objects.filter(
        fecha_reserva__range=[fecha_inicio_semana, fecha_fin_semana]
    ).count()
    
    # Reservas para hoy
    reservas_hoy = Reserva.objects.filter(fecha_reserva=hoy).count()
    
    # =============================================
    # 5. ESTADÍSTICAS DE ASISTENCIA (semana actual: Lunes a Viernes)
    # =============================================
    asistencia_semana = RegistroAsistencia.objects.filter(
        fechaClase__range=[fecha_inicio_semana, fecha_fin_semana]
    ).count()
    
    # Porcentaje promedio de asistencia
    if asistencia_semana > 0:
        total_estudiantes_asistencia = RegistroAsistenciaDetalle.objects.filter(
            registro_asistencia__fechaClase__range=[fecha_inicio_semana, fecha_fin_semana]
        ).count()
        
        presentes = RegistroAsistenciaDetalle.objects.filter(
            registro_asistencia__fechaClase__range=[fecha_inicio_semana, fecha_fin_semana],
            estado='PRESENTE'
        ).count()
        
        porcentaje_asistencia = (presentes / total_estudiantes_asistencia * 100) if total_estudiantes_asistencia > 0 else 0
    else:
        porcentaje_asistencia = 0
    
    # =============================================
    # 6. INFORMACIÓN DE GRUPOS ACTIVOS
    # =============================================
    # Grupos con profesor asignado
    grupos_con_profesor = GrupoCurso.objects.filter(profesor__isnull=False).count()
    grupos_sin_profesor = GrupoCurso.objects.filter(profesor__isnull=True).count()
    
    # Distribución por tipo de grupo
    grupos_teoria = GrupoTeoria.objects.count()
    grupos_laboratorio = GrupoLaboratorio.objects.count()
    
    # =============================================
    # 7. TOP 5 CURSOS CON MÁS MATRÍCULAS
    # =============================================
    top_cursos = Curso.objects.annotate(
        total_matriculas=Count('grupocurso__matricula', filter=Q(grupocurso__matricula__estado=True))
    ).order_by('-total_matriculas')[:5]
    
    # =============================================
    # 8. AULAS MÁS UTILIZADAS
    # =============================================
    aulas_populares = Aula.objects.annotate(
        total_bloques=Count('bloquehorario')
    ).order_by('-total_bloques')[:5]
    
    # =============================================
    # 9. PROFESORES MÁS ACTIVOS (por cantidad de grupos)
    # =============================================
    profesores_activos = Profesor.objects.annotate(
        total_grupos=Count('grupocurso')
    ).order_by('-total_grupos')[:5]

    cantidad_profesores_con_grupos = Profesor.objects.annotate(
            total_grupos=Count('grupocurso')
        ).filter(total_grupos__gt=0).count()
    
    contexto = {
        'perfil': admin_obj.perfil,
        'titulo': 'Panel de Control - Administrador',
        
        # Conteos básicos
        'conteo_profesores': conteo_profesores,
        'conteo_estudiantes': conteo_estudiantes,
        'conteo_secretarias': conteo_secretarias,
        'conteo_cursos': conteo_cursos,
        'conteo_aulas': conteo_aulas,
        
        # Matrículas
        'total_matriculas': total_matriculas,
        'matriculas_teoria': matriculas_teoria,
        'matriculas_lab': matriculas_lab,
        
        # Horarios
        'total_bloques': total_bloques,
        'bloques_aula_normal': bloques_aula_normal,
        'bloques_laboratorio': bloques_laboratorio,
        
        # Reservas
        'reservas_semana': reservas_semana,
        'reservas_hoy': reservas_hoy,
        
        # Asistencia
        'asistencia_semana': asistencia_semana,
        'porcentaje_asistencia': round(porcentaje_asistencia, 1),
        
        # Grupos
        'grupos_con_profesor': grupos_con_profesor,
        'grupos_sin_profesor': grupos_sin_profesor,
        'grupos_teoria': grupos_teoria,
        'grupos_laboratorio': grupos_laboratorio,
        
        # Listados
        'top_cursos': top_cursos,
        'aulas_populares': aulas_populares,
        'profesores_activos': profesores_activos,
        'profesores_grupos_asignados': cantidad_profesores_con_grupos,
        
        # Fechas para referencia - Ahora muestra "Lunes Actual - Viernes Actual"
        'hoy': hoy.strftime('%d/%m/%Y'),
        'semana_pasada': fecha_inicio_semana.strftime('%d/%m/%Y'),  # Lunes de esta semana
        'viernes_semana': fecha_fin_semana.strftime('%d/%m/%Y'),    # Viernes de esta semana
    }
    
    return render(request, 'usuarios/admin/dashboard_admin.html', contexto)

def mi_cuenta_admin(request):
    # Autenticación
    admin_obj, response = check_administrador_auth(request)
    if response: 
        return response

    perfil = admin_obj.perfil

    # -----------------------------
    # POST: cambio de contraseña
    # -----------------------------
    if request.method == 'POST':
        old_password = request.POST.get('old_password')
        new_password1 = request.POST.get('new_password1')
        new_password2 = request.POST.get('new_password2')

        # 1. Validar contraseña actual
        if old_password != perfil.password:
            messages.error(request, 'La contraseña actual ingresada es incorrecta.')
            return redirect('usuarios:mi_cuenta_admin')

        # 2. Validar nueva contraseña
        if new_password1 != new_password2:
            messages.error(request, 'La nueva contraseña y su confirmación no coinciden.')
            return redirect('usuarios:mi_cuenta_admin')

        if len(new_password1) < 6:
            messages.error(request, 'La nueva contraseña debe tener al menos 6 caracteres.')
            return redirect('usuarios:mi_cuenta_admin')

        if new_password1 == old_password:
            messages.warning(request, 'La nueva contraseña no puede ser igual a la anterior.')
            return redirect('usuarios:mi_cuenta_admin')

        # 3. Guardar
        try:
            perfil.password = new_password1
            perfil.save()
            messages.success(request, '¡Contraseña actualizada correctamente!')
        except Exception as e:
            messages.error(request, f'Error al guardar la nueva contraseña: {e}')

        return redirect('usuarios:mi_cuenta_admin')

    # -----------------------------
    # GET: mostrar página
    # -----------------------------
    contexto = {
        'perfil': perfil,
        'titulo': 'Mi Cuenta'
    }
    return render(request, 'usuarios/admin/mi_cuenta_admin.html', contexto)

def gestion_cursos_admin(request):
    perfil_obj, response = check_administrador_auth(request)
    if response: 
        return response

    # ======================================================================
    # 1. MANEJO DE SOLICITUDES AJAX (GET)
    # ======================================================================
    # Se usa para obtener dinámicamente los grupos de un curso y sus detalles (horarios, prof, etc.)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.method == 'GET':
        accion = request.GET.get('accion')
        
        # A. Obtener lista de grupos de teoría para el selector de edición
        if accion == 'obtener_grupos_curso':
            curso_id = request.GET.get('curso_id')
            # Filtramos solo aquellos que son Grupos de Teoría (tienen la relación grupoteoria)
            grupos = GrupoTeoria.objects.filter(grupo_curso__curso_id=curso_id).select_related('grupo_curso')
            data = [{'id': g.grupo_curso.id, 'grupo': g.grupo_curso.grupo} for g in grupos]
            return JsonResponse({'grupos': data})

        # B. Obtener el detalle completo de un grupo específico (Profesor, Capacidad, Horarios)
        if accion == 'obtener_detalle_grupo':
            grupo_id = request.GET.get('grupo_id')
            try:
                # Buscamos el GrupoCurso (que es la base)
                grupo_curso = GrupoCurso.objects.select_related('profesor__perfil').get(id=grupo_id)
                
                # Obtenemos sus bloques horarios
                bloques = BloqueHorario.objects.filter(grupo_curso=grupo_curso).select_related('aula')
                horarios = [{
                    'dia': b.dia,
                    'inicio': b.horaInicio.strftime('%H:%M:%S') if b.horaInicio else '',  # Agregar segundos
                    'fin': b.horaFin.strftime('%H:%M:%S') if b.horaFin else '',  # Agregar segundos
                    'aula_id': b.aula.id if b.aula else ''
                } for b in bloques]

                data = {
                    'id': grupo_curso.id,
                    'profesor_id': grupo_curso.profesor.perfil.id if grupo_curso.profesor else '',
                    'capacidad': grupo_curso.capacidad,
                    'horarios': horarios
                }
                return JsonResponse({'ok': True, 'data': data})
            except GrupoCurso.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Grupo no encontrado'})
            except Exception as e:
                return JsonResponse({'ok': False, 'msg': f'Error al obtener detalle: {str(e)}'})

        return JsonResponse({'ok': False, 'msg': 'Acción desconocida'})

    # ======================================================================
    # 2. MANEJO DE SOLICITUDES POST (CRUD)
    # ======================================================================
    if request.method == 'POST':
        accion = request.POST.get('accion')
        
        try:
            with transaction.atomic():
                
                # --- A. CREAR CURSO ---
                if accion == 'crear_curso':
                    Curso.objects.create(
                        id=request.POST.get('id'),
                        nombre=request.POST.get('nombre'),
                        creditos=request.POST.get('creditos'),
                        porcentajeEC1=request.POST.get('porcentajeEC1') or 0,
                        porcentajeEP1=request.POST.get('porcentajeEP1') or 0,
                        porcentajeEC2=request.POST.get('porcentajeEC2') or 0,
                        porcentajeEP2=request.POST.get('porcentajeEP2') or 0,
                        porcentajeEC3=request.POST.get('porcentajeEC3') or 0,
                        porcentajeEP3=request.POST.get('porcentajeEP3') or 0,
                        # URLs se inicializan en Null/Blank por defecto en el modelo
                    )
                    messages.success(request, "Curso creado exitosamente.")

                # --- B. EDITAR CURSO ---
                elif accion == 'editar_curso':
                    curso = get_object_or_404(Curso, id=request.POST.get('id'))
                    curso.nombre = request.POST.get('nombre')
                    curso.creditos = request.POST.get('creditos')
                    curso.porcentajeEC1 = request.POST.get('porcentajeEC1') or 0
                    curso.porcentajeEP1 = request.POST.get('porcentajeEP1') or 0
                    curso.porcentajeEC2 = request.POST.get('porcentajeEC2') or 0
                    curso.porcentajeEP2 = request.POST.get('porcentajeEP2') or 0
                    curso.porcentajeEC3 = request.POST.get('porcentajeEC3') or 0
                    curso.porcentajeEP3 = request.POST.get('porcentajeEP3') or 0
                    
                    # Lógica para manejar archivos subidos (AGREGAR ESTO)
                    # 1. Sílabo
                    if 'silabo' in request.FILES:
                        curso.silabo_url = request.FILES['silabo']
                    
                    # 2. Archivos de Fase 1
                    if 'fase1alta' in request.FILES:
                        curso.Fase1notaAlta_url = request.FILES['fase1alta']
                    if 'fase1media' in request.FILES:
                        curso.Fase1notaMedia_url = request.FILES['fase1media']
                    if 'fase1baja' in request.FILES:
                        curso.Fase1notaBaja_url = request.FILES['fase1baja']
                    
                    # 3. Archivos de Fase 2
                    if 'fase2alta' in request.FILES:
                        curso.Fase2notaAlta_url = request.FILES['fase2alta']
                    if 'fase2media' in request.FILES:
                        curso.Fase2notaMedia_url = request.FILES['fase2media']
                    if 'fase2baja' in request.FILES:
                        curso.Fase2notaBaja_url = request.FILES['fase2baja']
                    
                    # 4. Archivos de Fase 3
                    if 'fase3alta' in request.FILES:
                        curso.Fase3notaAlta_url = request.FILES['fase3alta']
                    if 'fase3media' in request.FILES:
                        curso.Fase3notaMedia_url = request.FILES['fase3media']
                    if 'fase3baja' in request.FILES:
                        curso.Fase3notaBaja_url = request.FILES['fase3baja']
                    
                    # Lógica para eliminar archivos si se marcaron los checkboxes (MANTENER ESTO)
                    if request.POST.get('eliminar_silabo') == '1': 
                        curso.silabo_url = None
                    if request.POST.get('eliminar_fase1alta') == '1': 
                        curso.Fase1notaAlta_url = None
                    if request.POST.get('eliminar_fase1media') == '1': 
                        curso.Fase1notaMedia_url = None
                    if request.POST.get('eliminar_fase1baja') == '1': 
                        curso.Fase1notaBaja_url = None
                    if request.POST.get('eliminar_fase2alta') == '1': 
                        curso.Fase2notaAlta_url = None
                    if request.POST.get('eliminar_fase2media') == '1': 
                        curso.Fase2notaMedia_url = None
                    if request.POST.get('eliminar_fase2baja') == '1': 
                        curso.Fase2notaBaja_url = None
                    if request.POST.get('eliminar_fase3alta') == '1': 
                        curso.Fase3notaAlta_url = None
                    if request.POST.get('eliminar_fase3media') == '1': 
                        curso.Fase3notaMedia_url = None
                    if request.POST.get('eliminar_fase3baja') == '1': 
                        curso.Fase3notaBaja_url = None
                    
                    curso.save()
                    messages.success(request, "Información del curso actualizada.")

                # --- C. ELIMINAR CURSO ---
                elif accion == 'eliminar_curso':
                    curso_id = request.POST.get('curso_id')
                    # Verificar si tiene grupos asignados antes de eliminar
                    if GrupoCurso.objects.filter(curso_id=curso_id).exists():
                        messages.error(request, "No se puede eliminar el curso debido a que hay grupos asignados.")
                    else:
                        Curso.objects.filter(id=curso_id).delete()
                        messages.success(request, "Curso eliminado correctamente.")

                # --- D. CREAR GRUPO TEORÍA ---
                elif accion == 'crear_grupo_teoria':
                    curso_id = request.POST.get('curso_id')
                    letra_grupo = request.POST.get('grupo', '').upper() # A, B, C...
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    # DEBUG: Mostrar lo que llega
                    print(f"DEBUG - horarios_json recibido: {horarios_json}")
                    
                    # Generación del ID Compuesto: CURSOID + LETRA
                    nuevo_id = f"{curso_id}{letra_grupo}"
                    
                    if GrupoCurso.objects.filter(id=nuevo_id).exists():
                        messages.error(request, f"El Grupo {letra_grupo} para este curso ya existe (ID: {nuevo_id}).")
                    else:
                        try:
                            # Validar horarios_json
                            if not horarios_json or horarios_json.strip() == '':
                                raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                            
                            # 1. Parsear JSON
                            horarios = json.loads(horarios_json)
                            
                            # Validar que haya al menos un horario
                            if not isinstance(horarios, list) or len(horarios) == 0:
                                raise ValueError("Debe agregar al menos un bloque de horario.")
                            
                            # 2. Iniciar Transacción Atómica
                            with transaction.atomic():
                                # Validación de existencia de objetos FK y conversión de capacidad
                                profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                                curso = Curso.objects.get(id=curso_id)
                                capacidad_int = int(capacidad)
                                
                                # 3. Validar cada horario
                                for i, h in enumerate(horarios, 1):
                                    try:
                                        aula_obj = Aula.objects.get(id=h['aula_id'])
                                    except ObjectDoesNotExist:
                                        raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                    
                                    # Validar campos requeridos
                                    if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                        raise ValueError(f"El horario {i} tiene campos incompletos.")
                                    
                                    # Convertir tiempos
                                    try:
                                        inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                        fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                    except ValueError:
                                        raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                    
                                    # Validar que inicio < fin
                                    if inicio_time >= fin_time:
                                        raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                    
                                    # 3.1 Verificar cruce con horarios existentes en la misma aula
                                    horarios_cruzados_aula = BloqueHorario.objects.filter(
                                        aula=aula_obj,
                                        dia=h['dia']
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_cruzados_aula.exists():
                                        conflicto = horarios_cruzados_aula.first()
                                        curso_conflicto = conflicto.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto.grupo_curso.grupo
                                        raise ValueError(
                                            f"Conflicto de horario en aula {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                            f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                        )
                                    
                                    # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                    if profesor:
                                        # Buscar horarios donde el docente ya tenga clases asignadas
                                        horarios_docente = BloqueHorario.objects.filter(
                                            grupo_curso__profesor=profesor,
                                            dia=h['dia']
                                        ).exclude(
                                            Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                        )
                                        
                                        if horarios_docente.exists():
                                            conflicto_docente = horarios_docente.first()
                                            curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                            grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                            aula_conflicto = conflicto_docente.aula.id
                                            raise ValueError(
                                                f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                                f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                            )

                                # 4. Crear el GrupoCurso base
                                nuevo_grupo = GrupoCurso.objects.create(
                                    id=nuevo_id,
                                    curso=curso,
                                    profesor=profesor,
                                    grupo=letra_grupo,
                                    capacidad=capacidad_int
                                )
                                
                                # 5. Crear la especialización GrupoTeoria
                                GrupoTeoria.objects.create(grupo_curso=nuevo_grupo)
                                
                                # 6. Crear los Bloques de Horario
                                for h in horarios:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                    BloqueHorario.objects.create(
                                        dia=h['dia'],
                                        horaInicio=h['inicio'],
                                        horaFin=h['fin'],
                                        grupo_curso=nuevo_grupo,
                                        aula=aula_obj
                                    )
                            
                            messages.success(request, f"Grupo de teoría {letra_grupo} creado exitosamente.")

                        except ObjectDoesNotExist as e:
                            messages.error(request, f"Error de referencia: {str(e)}")
                        except ValueError as e:
                            messages.error(request, f"Error en los datos: {str(e)}")
                        except json.JSONDecodeError as e:
                            messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                        except Exception as e:
                            messages.error(request, f"Error inesperado al crear el grupo: {str(e)}")

                # --- E. EDITAR GRUPO TEORÍA (GESTIONAR) ---
                elif accion == 'editar_grupo_teoria':
                    grupo_id = request.POST.get('grupo_id') # ID completo (Ej: INF101A)
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    # 1. Recuperar GrupoCurso o lanzar 404 si no existe
                    grupo_curso = get_object_or_404(GrupoCurso, id=grupo_id)
                    
                    try:
                        # Validar horarios_json
                        if not horarios_json or horarios_json.strip() == '':
                            raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                        
                        # Parsear JSON
                        horarios = json.loads(horarios_json)
                        
                        # Validar que haya al menos un horario
                        if not isinstance(horarios, list) or len(horarios) == 0:
                            raise ValueError("Debe agregar al menos un bloque de horario.")
                        
                        # 2. Iniciar Transacción Atómica
                        with transaction.atomic():
                            # Validación de existencia de objetos FK y conversión de capacidad
                            profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                            capacidad_int = int(capacidad) # Si no es un entero, lanza ValueError
                            
                            # 3. Validar cada horario antes de eliminar los existentes
                            for i, h in enumerate(horarios, 1):
                                try:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                except ObjectDoesNotExist:
                                    raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                
                                # Validar campos requeridos
                                if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                    raise ValueError(f"El horario {i} tiene campos incompletos.")
                                
                                # Convertir tiempos
                                try:
                                    inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                    fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                except ValueError:
                                    raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                
                                # Validar que inicio < fin
                                if inicio_time >= fin_time:
                                    raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                
                                # 3.1 Verificar cruce con horarios existentes en la misma aula
                                # Excluir los horarios del grupo actual que estamos editando
                                horarios_cruzados_aula = BloqueHorario.objects.filter(
                                    aula=aula_obj,
                                    dia=h['dia']
                                ).exclude(
                                    grupo_curso=grupo_curso  # Excluir los horarios del grupo actual
                                ).exclude(
                                    Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                )
                                
                                if horarios_cruzados_aula.exists():
                                    conflicto = horarios_cruzados_aula.first()
                                    curso_conflicto = conflicto.grupo_curso.curso.nombre
                                    grupo_conflicto = conflicto.grupo_curso.grupo
                                    raise ValueError(
                                        f"Conflicto de horario en aula {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                        f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                    )
                                
                                # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                if profesor:
                                    # Buscar horarios donde el docente ya tenga clases asignadas
                                    # Excluir los horarios del grupo actual que estamos editando
                                    horarios_docente = BloqueHorario.objects.filter(
                                        grupo_curso__profesor=profesor,
                                        dia=h['dia']
                                    ).exclude(
                                        grupo_curso=grupo_curso  # Excluir los horarios del grupo actual
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_docente.exists():
                                        conflicto_docente = horarios_docente.first()
                                        curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                        aula_conflicto = conflicto_docente.aula.id
                                        raise ValueError(
                                            f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                            f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                        )

                            # 4. Actualizar datos básicos
                            grupo_curso.profesor = profesor
                            grupo_curso.capacidad = capacidad_int
                            grupo_curso.save()
                            
                            # 5. Actualizar horarios: Estrategia de reemplazo total (Borrar y crear)
                            BloqueHorario.objects.filter(grupo_curso=grupo_curso).delete()
                            
                            for h in horarios:
                                aula_obj = Aula.objects.get(id=h['aula_id'])
                                BloqueHorario.objects.create(
                                    dia=h['dia'],
                                    horaInicio=h['inicio'],
                                    horaFin=h['fin'],
                                    grupo_curso=grupo_curso,
                                    aula=aula_obj
                                )
                        
                        messages.success(request, "Grupo actualizado correctamente.")
                    
                    except ObjectDoesNotExist as e:
                        # Captura error si el Profesor no se encuentra.
                        messages.error(request, f"Error de referencia: {str(e)}")
                    except ValueError as e:
                        # Captura errores de conversión (ej. capacidad no es int) o errores de Aula
                        messages.error(request, f"Error en los datos proporcionados: {str(e)}")
                    except json.JSONDecodeError as e:
                        messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                    except Exception as e:
                        # Captura errores genéricos, como JSON mal formado
                        messages.error(request, f"Error inesperado al actualizar el grupo: {str(e)}")

                # --- F. ELIMINAR GRUPO ---
                elif accion == 'eliminar_grupo':
                    grupo_id = request.POST.get('grupo_id')
                    # Al eliminar GrupoCurso, el CASCADE elimina GrupoTeoria y BloqueHorario
                    GrupoCurso.objects.filter(id=grupo_id).delete()
                    messages.success(request, "Grupo eliminado correctamente.")

        except IntegrityError as e:
            messages.error(request, f"Error de integridad en la base de datos: {str(e)}")
        except Exception as e:
            messages.error(request, f"Ocurrió un error inesperado: {str(e)}")
            
        return redirect('usuarios:gestion_cursos_admin')

    # ======================================================================
    # 3. LÓGICA GET (RENDERIZADO DE LA PÁGINA)
    # ======================================================================
    
    # Obtener cursos con anotaciones para los contadores
    # num_grupos_teoria: Cuenta cuántos grupos tiene que sean de teoría
    # num_matriculados: Suma de matrículas en grupos que son de teoría
    cursos = Curso.objects.annotate(
        num_grupos_teoria=Count('grupocurso__grupoteoria', distinct=True),
        num_matriculados=Count('grupocurso__matricula', 
                               filter=Q(grupocurso__grupoteoria__isnull=False), 
                               distinct=True)
    ).order_by('id')

    # Listas auxiliares para llenar los selectores de los modales
    profesores_teoria = Profesor.objects.filter(es_teoria=True).select_related('perfil').order_by('perfil__nombre')
    aulas = Aula.objects.all().order_by('id')

    contexto = {
        'perfil': perfil_obj.perfil, 
        'titulo': 'Gestión de Cursos',
        'cursos': cursos,
        'profesores': profesores_teoria,
        'aulas': aulas
    }
    return render(request, 'usuarios/admin/gestion_cursos_admin.html', contexto)

def ver_horarios_clases_admin(request):
    perfil_obj, response = check_administrador_auth(request)
    if response: 
        return response

    # ======================================================================
    # 1. MANEJO DE SOLICITUDES AJAX (GET) - Para cargar horarios por aula
    # ======================================================================
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.method == 'GET':
        accion = request.GET.get('accion')
        
        # Obtener horarios de un aula específica
        if accion == 'obtener_horarios_aula':
            aula_id = request.GET.get('aula_id')
            
            try:
                # Obtener el aula
                aula = Aula.objects.get(id=aula_id)
                
                # COLORES (excluyendo colores muy oscuros)
                COLOR_OPTIONS = [
                    'bg-primary', 'bg-success', 'bg-info', 
                    'bg-warning', 'bg-danger', 'bg-secondary'
                ]
                curso_colores = {}
                color_index = 0
                
                # Obtener todas las actividades para esta aula
                horario_completo = []
                puntos_corte = set()
                
                # 1. OBTENER CLASES REGULARES (GrupoCurso con BloqueHorario)
                bloques_clases = BloqueHorario.objects.filter(aula=aula).select_related(
                    'grupo_curso__curso',
                    'grupo_curso__profesor__perfil'
                ).order_by('horaInicio')
                
                print(f"DEBUG - Total bloques encontrados: {bloques_clases.count()}")
                
                for bloque in bloques_clases:
                    # Determinar tipo: Teoría o Laboratorio
                    tipo = "CLS"  # Por defecto
                    try:
                        # Verificar si es grupo de teoría
                        bloque.grupo_curso.grupoteoria
                        tipo = "TEO"
                    except:
                        try:
                            # Verificar si es grupo de laboratorio
                            bloque.grupo_curso.grupolaboratorio
                            tipo = "LAB"
                        except:
                            tipo = "CLS"  # Clase genérica
                    
                    # Profesor info
                    profesor_nombre = ""
                    if bloque.grupo_curso.profesor and bloque.grupo_curso.profesor.perfil:
                        profesor_nombre = bloque.grupo_curso.profesor.perfil.nombre
                    
                    print(f"DEBUG - Bloque: {bloque.grupo_curso.curso.nombre} - {bloque.dia} - {bloque.horaInicio} a {bloque.horaFin} - Tipo: {tipo}")
                    
                    # Agregar al horario completo
                    horario_completo.append({
                        'tipo_actividad': 'CLASE',
                        'bloque': bloque,
                        'horaInicio': bloque.horaInicio,
                        'horaFin': bloque.horaFin,
                        'dia': bloque.dia,
                        'curso': bloque.grupo_curso.curso,
                        'profesor_nombre': profesor_nombre,
                        'grupo': bloque.grupo_curso.grupo,
                        'tipo': tipo
                    })
                    
                    # Agregar puntos de corte (sin segundos/microsegundos)
                    puntos_corte.add(bloque.horaInicio.replace(second=0, microsecond=0))
                    puntos_corte.add(bloque.horaFin.replace(second=0, microsecond=0))
                    
                    # Asignar color al curso
                    curso_id = bloque.grupo_curso.curso.id
                    if curso_id not in curso_colores:
                        curso_colores[curso_id] = COLOR_OPTIONS[color_index % len(COLOR_OPTIONS)]
                        color_index += 1
                
                # 2. OBTENER RESERVAS PARA ESTA AULA
                # Obtener fecha actual para filtrar reservas de esta semana
                hoy = date.today()
                inicio_semana = hoy - timedelta(days=hoy.weekday())  # Lunes
                fin_semana = inicio_semana + timedelta(days=4)  # Viernes
                
                reservas = Reserva.objects.filter(
                    aula=aula,
                    fecha_reserva__range=[inicio_semana, fin_semana]
                ).select_related('profesor__perfil')
                
                print(f"DEBUG - Total reservas encontradas: {reservas.count()}")
                
                for reserva in reservas:
                    # Profesor info
                    profesor_nombre = ""
                    if reserva.profesor and reserva.profesor.perfil:
                        profesor_nombre = reserva.profesor.perfil.nombre
                    
                    # Determinar día de la semana
                    dia_semana = reserva.fecha_reserva.weekday()
                    dias_semana = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                    dia = dias_semana[dia_semana] if dia_semana < 5 else None
                    
                    if dia:
                        horario_completo.append({
                            'tipo_actividad': 'RESERVA',
                            'reserva': reserva,
                            'horaInicio': reserva.hora_inicio,
                            'horaFin': reserva.hora_fin,
                            'dia': dia,
                            'curso': None,
                            'profesor_nombre': profesor_nombre,
                            'motivo': "RESERVA",
                            'tipo': 'RES'
                        })
                        
                        # Agregar puntos de corte
                        puntos_corte.add(reserva.hora_inicio.replace(second=0, microsecond=0))
                        puntos_corte.add(reserva.hora_fin.replace(second=0, microsecond=0))
                
                print(f"DEBUG - Total actividades: {len(horario_completo)}")
                print(f"DEBUG - Puntos de corte: {len(puntos_corte)}")
                
                # ======================================================================
                # GENERAR HORAS DINÁMICAS
                # ======================================================================
                # Convertir puntos de corte de 'time' a 'datetime'
                dummy_date = hoy
                puntos_corte_dt = sorted(list(set([
                    datetime.combine(dummy_date, t) for t in puntos_corte
                ])))
                
                # Crear intervalos de hora
                horas = []
                hora_actual = None
                
                for dt_siguiente in puntos_corte_dt:
                    if hora_actual is None:
                        hora_actual = dt_siguiente
                        continue
                    
                    dt_inicio_fila = hora_actual
                    dt_fin_fila = dt_siguiente

                    duracion_minutos = (dt_fin_fila - dt_inicio_fila).total_seconds() / 60
                    if duracion_minutos < 11:
                        hora_actual = dt_siguiente
                        continue
                    
                    if dt_inicio_fila >= dt_fin_fila:
                        hora_actual = dt_siguiente
                        continue
                    
                    # Formatear rango de hora
                    hora_inicio_str = dt_inicio_fila.strftime("%H:%M")
                    hora_fin_str = dt_fin_fila.strftime("%H:%M")
                    horas.append(f"{hora_inicio_str} - {hora_fin_str}")
                    
                    hora_actual = dt_siguiente
                
                # Si no hay actividades, usar horas por defecto
                if not horas:
                    hora_actual = datetime.strptime('07:00', '%H:%M')
                    hora_final = datetime.strptime('21:00', '%H:%M')
                    
                    while hora_actual < hora_final:
                        hora_str = hora_actual.strftime('%H:%M')
                        hora_siguiente = hora_actual + timedelta(hours=1)
                        horas.append(f"{hora_str} - {hora_siguiente.strftime('%H:%M')}")
                        hora_actual = hora_siguiente
                
                print(f"DEBUG - Horas generadas: {horas}")
                
                # ======================================================================
                # ESTRUCTURAR DATOS DEL HORARIO
                # ======================================================================
                dias = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                horario_data = {}
                
                # Para cada intervalo de hora
                for hora_idx, rango_hora in enumerate(horas):
                    hora_inicio_str, hora_fin_str = rango_hora.split(" - ")
                    
                    # Convertir a datetime para comparar
                    try:
                        hora_inicio_dt = datetime.strptime(hora_inicio_str, "%H:%M").time()
                        hora_fin_dt = datetime.strptime(hora_fin_str, "%H:%M").time()
                    except:
                        # Si hay error, saltar este intervalo
                        continue
                    
                    horario_data[str(hora_idx)] = {}
                    
                    # Para cada día
                    for dia_idx, dia_key in enumerate(dias):
                        actividad_en_slot = None
                        
                        # Buscar actividad que coincida en este slot
                        for actividad in horario_completo:
                            # Verificar si es el día correcto
                            if actividad['dia'] != dia_key:
                                continue
                            
                            # Verificar si la actividad cubre este intervalo
                            actividad_inicio = actividad['horaInicio']
                            actividad_fin = actividad['horaFin']
                            
                            if actividad_inicio <= hora_inicio_dt and actividad_fin > hora_inicio_dt:
                                print(f"DEBUG - ¡COINCIDE! Actividad encontrada para {dia_key} {rango_hora}")
                                
                                # Es una clase regular
                                if actividad['tipo_actividad'] == 'CLASE':
                                    curso = actividad['curso']
                                    curso_id = str(curso.id)
                                    
                                    actividad_en_slot = {
                                        'tipo': actividad['tipo'],
                                        'nombre': curso.nombre,
                                        'codigo_curso': curso_id,
                                        'codigo_grupo': f"{curso_id}-{actividad['grupo']}",
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': curso_colores.get(curso_id, 'bg-secondary'),
                                        'hora_inicio': actividad_inicio.strftime("%H:%M:%S"),
                                        'hora_fin': actividad_fin.strftime("%H:%M:%S"),
                                        'es_reserva': False
                                    }
                                
                                # Es una reserva
                                else:
                                    actividad_en_slot = {
                                        'tipo': 'RES',
                                        'nombre': actividad.get('motivo', 'Reserva de Aula'),
                                        'codigo_curso': 'RES',
                                        'codigo_grupo': 'RESERVA',
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': 'bg-secondary',
                                        'hora_inicio': actividad_inicio.strftime("%H:%M:%S"),
                                        'hora_fin': actividad_fin.strftime("%H:%M:%S"),
                                        'es_reserva': True,
                                        'motivo': actividad.get('motivo', '')
                                    }
                                break
                        
                        horario_data[str(hora_idx)][str(dia_idx)] = actividad_en_slot
                        if actividad_en_slot:
                            print(f"DEBUG - Asignado: [{hora_idx}][{dia_idx}] = {actividad_en_slot['nombre']}")
                
                return JsonResponse({
                    'ok': True,
                    'aula': {
                        'id': aula.id,
                        'tipo': aula.tipo,
                    },
                    'horas': horas,
                    'horario_data': horario_data,
                    'curso_colores': curso_colores
                })
                
            except Aula.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Aula no encontrada'})
            except Exception as e:
                import traceback
                print(f"ERROR: {str(e)}")
                print(traceback.format_exc())
                return JsonResponse({'ok': False, 'msg': f'Error al cargar horarios: {str(e)}'})
        
        # Exportar horario a PDF
        elif accion == 'exportar_pdf':
            aula_id = request.GET.get('aula_id')
            
            try:
                aula = Aula.objects.get(id=aula_id)
                
                # USAR LA MISMA LÓGICA QUE EN LA VISTA PRINCIPAL
                from django.utils import timezone
                hoy = date.today()
                inicio_semana = hoy - timedelta(days=hoy.weekday())
                fin_semana = inicio_semana + timedelta(days=4)
                
                # 1. Obtener datos EXACTAMENTE como en la vista principal
                COLOR_OPTIONS = ['bg-primary', 'bg-success', 'bg-info', 'bg-warning', 'bg-danger', 'bg-secondary']
                curso_colores = {}
                color_index = 0
                
                horario_completo = []
                puntos_corte = set()
                
                # BloqueHorario para esta aula
                bloques_clases = BloqueHorario.objects.filter(aula=aula).select_related(
                    'grupo_curso__curso',
                    'grupo_curso__profesor__perfil'
                ).order_by('horaInicio')
                
                for bloque in bloques_clases:
                    tipo = "TEO"
                    try:
                        bloque.grupo_curso.grupoteoria
                    except:
                        tipo = "LAB"
                    
                    profesor_nombre = ""
                    if bloque.grupo_curso.profesor and bloque.grupo_curso.profesor.perfil:
                        profesor_nombre = bloque.grupo_curso.profesor.perfil.nombre
                    
                    horario_completo.append({
                        'tipo_actividad': 'CLASE',
                        'bloque': bloque,
                        'horaInicio': bloque.horaInicio,
                        'horaFin': bloque.horaFin,
                        'dia': bloque.dia,
                        'curso': bloque.grupo_curso.curso,
                        'profesor_nombre': profesor_nombre,
                        'grupo': bloque.grupo_curso.grupo,
                        'tipo': tipo
                    })
                    
                    puntos_corte.add(bloque.horaInicio.replace(second=0, microsecond=0))
                    puntos_corte.add(bloque.horaFin.replace(second=0, microsecond=0))
                    
                    curso_id = bloque.grupo_curso.curso.id
                    if curso_id not in curso_colores:
                        curso_colores[curso_id] = COLOR_OPTIONS[color_index % len(COLOR_OPTIONS)]
                        color_index += 1
                
                # Reservas
                reservas = Reserva.objects.filter(
                    aula=aula,
                    fecha_reserva__range=[inicio_semana, fin_semana]
                ).select_related('profesor__perfil')
                
                for reserva in reservas:
                    profesor_nombre = ""
                    if reserva.profesor and reserva.profesor.perfil:
                        profesor_nombre = reserva.profesor.perfil.nombre
                    
                    dia_semana = reserva.fecha_reserva.weekday()
                    dias_semana = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                    dia = dias_semana[dia_semana] if dia_semana < 5 else None
                    
                    if dia:
                        horario_completo.append({
                            'tipo_actividad': 'RESERVA',
                            'reserva': reserva,
                            'horaInicio': reserva.hora_inicio,
                            'horaFin': reserva.hora_fin,
                            'dia': dia,
                            'curso': None,
                            'profesor_nombre': profesor_nombre,
                            'motivo': 'RESERVA',
                            'tipo': 'RES'
                        })
                        
                        puntos_corte.add(reserva.hora_inicio.replace(second=0, microsecond=0))
                        puntos_corte.add(reserva.hora_fin.replace(second=0, microsecond=0))
                
                # 2. Generar horas dinámicas (MISMA LÓGICA)
                dummy_date = hoy
                puntos_corte_dt = sorted(list(set([
                    datetime.combine(dummy_date, t) for t in puntos_corte
                ])))
                
                horas = []
                hora_actual = None
                
                for dt_siguiente in puntos_corte_dt:
                    if hora_actual is None:
                        hora_actual = dt_siguiente
                        continue
                    
                    dt_inicio_fila = hora_actual
                    dt_fin_fila = dt_siguiente

                    duracion_minutos = (dt_fin_fila - dt_inicio_fila).total_seconds() / 60
                    if duracion_minutos < 11:
                        hora_actual = dt_siguiente
                        continue
                    
                    if dt_inicio_fila >= dt_fin_fila:
                        hora_actual = dt_siguiente
                        continue
                    
                    hora_inicio_str = dt_inicio_fila.strftime("%H:%M")
                    hora_fin_str = dt_fin_fila.strftime("%H:%M")
                    horas.append(f"{hora_inicio_str} - {hora_fin_str}")
                    
                    hora_actual = dt_siguiente
                
                if not horas:
                    hora_actual = datetime.strptime('07:00', '%H:%M')
                    hora_final = datetime.strptime('21:00', '%H:%M')
                    
                    while hora_actual < hora_final:
                        hora_str = hora_actual.strftime('%H:%M')
                        hora_siguiente = hora_actual + timedelta(hours=1)
                        horas.append(f"{hora_str} - {hora_siguiente.strftime('%H:%M')}")
                        hora_actual = hora_siguiente
                
                print(f"PDF DEBUG - Horas dinámicas: {len(horas)} intervalos")
                print(f"PDF DEBUG - Primeras horas: {horas[:3]}")
                
                # 3. Estructurar datos (MISMA LÓGICA)
                dias = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
                dias_display = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes']
                horario_data = {}
                
                # Para cada intervalo de hora
                for hora_idx, rango_hora in enumerate(horas):
                    hora_inicio_str, hora_fin_str = rango_hora.split(" - ")
                    
                    try:
                        hora_inicio_dt = datetime.strptime(hora_inicio_str, "%H:%M").time()
                        hora_fin_dt = datetime.strptime(hora_fin_str, "%H:%M").time()
                    except:
                        continue
                    
                    horario_data[str(hora_idx)] = {}
                    
                    # Para cada día
                    for dia_idx, dia_key in enumerate(dias):
                        actividad_en_slot = None
                        
                        # Buscar actividad que coincida en este slot
                        for actividad in horario_completo:
                            # Verificar si es el día correcto
                            if actividad['dia'] != dia_key:
                                continue
                            
                            # Convertir horas de actividad
                            act_inicio = datetime.combine(dummy_date, actividad['horaInicio'])
                            act_fin = datetime.combine(dummy_date, actividad['horaFin'])
                            hora_inicio_dt_full = datetime.combine(dummy_date, hora_inicio_dt)
                            
                            # MISMA LÓGICA EXACTA: act_inicio <= hora_inicio_dt_full AND act_fin > hora_inicio_dt_full
                            if act_inicio <= hora_inicio_dt_full and act_fin > hora_inicio_dt_full:
                                
                                # Es una clase regular
                                if actividad['tipo_actividad'] == 'CLASE':
                                    curso = actividad['curso']
                                    curso_id = str(curso.id)
                                    
                                    actividad_en_slot = {
                                        'tipo': actividad['tipo'],
                                        'nombre': curso.nombre,
                                        'codigo_curso': curso_id,
                                        'codigo_grupo': f"{curso_id}-{actividad['grupo']}",
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': curso_colores.get(curso_id, 'bg-secondary'),
                                        'hora_inicio': actividad['horaInicio'].strftime("%H:%M"),
                                        'hora_fin': actividad['horaFin'].strftime("%H:%M"),
                                        'es_reserva': False
                                    }
                                
                                # Es una reserva
                                else:
                                    actividad_en_slot = {
                                        'tipo': 'RES',
                                        'nombre': actividad.get('motivo', 'Reserva de Aula'),
                                        'codigo_curso': 'RES',
                                        'codigo_grupo': 'RESERVA',
                                        'profesor': actividad['profesor_nombre'],
                                        'aula_id': aula.id,
                                        'color': 'bg-secondary',
                                        'hora_inicio': actividad['horaInicio'].strftime("%H:%M"),
                                        'hora_fin': actividad['horaFin'].strftime("%H:%M"),
                                        'es_reserva': True,
                                        'motivo': actividad.get('motivo', '')
                                    }
                                break
                        
                        horario_data[str(hora_idx)][str(dia_idx)] = actividad_en_slot
                
                # DEBUG
                print(f"PDF DEBUG - Estructura final:")
                for hora_idx in range(len(horas)):
                    for dia_idx in range(len(dias)):
                        celda = horario_data[str(hora_idx)][str(dia_idx)]
                        if celda:
                            print(f"  [{hora_idx}][{dia_idx}]: {celda['nombre'][:20]}")
                
                curso_colores_map = {}
                for i, (curso_id, _) in enumerate(curso_colores.items()):
                    curso_colores_map[str(curso_id)] = f"color-{i % 10}"

                # Preparar contexto
                contexto = {
                    'aula': aula,
                    'horas': horas,
                    'horas_range': range(len(horas)),
                    'num_horas': len(horas),
                    'dias': dias_display,
                    'dias_range': range(len(dias)),
                    'horario_data': horario_data,
                    'curso_colores_map': curso_colores_map,
                    'fecha_emision': hoy.strftime("%d/%m/%Y"),
                    'semana_actual': f"{inicio_semana.strftime('%d/%m')} - {fin_semana.strftime('%d/%m')}"
                }
                
                # Renderizar template
                html_string = render_to_string('usuarios/secretaria/horario_aula_pdf.html', contexto)
                
                # Crear PDF
                response = HttpResponse(content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="horario_aula_{aula.id}_{hoy.strftime("%Y%m%d")}.pdf"'
                
                pisa_status = pisa.CreatePDF(
                    html_string, 
                    dest=response,
                    encoding='UTF-8'
                )
                
                if pisa_status.err:
                    return HttpResponse('Error al generar PDF', status=500)
                
                return response
                
            except Aula.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Aula no encontrada'})
            except Exception as e:
                import traceback
                print(f"ERROR PDF: {str(e)}")
                print(traceback.format_exc())
                return JsonResponse({'ok': False, 'msg': f'Error al generar PDF: {str(e)}'})

    # ======================================================================
    # 2. LÓGICA GET (RENDERIZADO DE LA PÁGINA INICIAL)
    # ======================================================================
    
    # Obtener todas las aulas ordenadas
    aulas = Aula.objects.all().order_by('id')
    
    # Definir horas del horario por defecto (para mostrar estructura inicial)
    horas = []
    hora_actual = datetime.strptime('07:00', '%H:%M')
    hora_final = datetime.strptime('21:00', '%H:%M')
    
    while hora_actual < hora_final:
        hora_str = hora_actual.strftime('%H:%M')
        hora_siguiente = hora_actual + timedelta(hours=1)
        horas.append(f"{hora_str} - {hora_siguiente.strftime('%H:%M')}")
        hora_actual = hora_siguiente
    
    # Días de la semana
    dias = ['LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES']
    
    # Fecha actual para mostrar en la página
    from django.utils import timezone
    semana_actual = timezone.now()
    
    contexto = {
        'perfil': perfil_obj.perfil, 
        'titulo': 'Visualización de Horarios por Aula',
        'aulas': aulas,
        'horas': horas,
        'dias': dias,
        'curso_colores': {},
        'aula_seleccionada': None,
        'semana_actual': semana_actual
    }
    
    return render(request, 'usuarios/admin/ver_horarios_clases_admin.html', contexto)

def gestion_laboratorios_admin(request):
    perfil_obj, response = check_administrador_auth(request)
    if response: 
        return response

    # ======================================================================
    # 1. MANEJO DE SOLICITUDES AJAX (GET)
    # ======================================================================
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' and request.method == 'GET':
        accion = request.GET.get('accion')
        
        # A. Obtener lista de cursos que tienen grupos de teoría (requisito para laboratorios)
        if accion == 'obtener_cursos_con_teoria':
            # Filtramos cursos que tienen al menos un grupo de teoría
            cursos_con_teoria = Curso.objects.filter(
                grupocurso__grupoteoria__isnull=False
            ).distinct().order_by('id')
            
            data = [{'id': c.id, 'nombre': c.nombre} for c in cursos_con_teoria]
            return JsonResponse({'cursos': data})
        
        # B. Obtener grupos de teoría de un curso específico
        if accion == 'obtener_grupos_teoria_curso':
            curso_id = request.GET.get('curso_id')
            # Solo grupos de teoría
            grupos = GrupoTeoria.objects.filter(grupo_curso__curso_id=curso_id).select_related('grupo_curso')
            data = [{'id': g.grupo_curso.id, 'grupo': g.grupo_curso.grupo} for g in grupos]
            return JsonResponse({'grupos_teoria': data})

        # C. Obtener lista de laboratorios de un curso específico
        if accion == 'obtener_laboratorios_curso':
            curso_id = request.GET.get('curso_id')
            # Filtramos solo grupos de laboratorio
            laboratorios = GrupoLaboratorio.objects.filter(
                grupo_curso__curso_id=curso_id
            ).select_related('grupo_curso')
            data = [{'id': g.grupo_curso.id, 'grupo': g.grupo_curso.grupo} for g in laboratorios]
            return JsonResponse({'laboratorios': data})

        # D. Obtener el detalle completo de un laboratorio específico
        if accion == 'obtener_detalle_laboratorio':
            laboratorio_id = request.GET.get('laboratorio_id')
            try:
                # Buscamos el GrupoCurso (que es la base)
                grupo_curso = GrupoCurso.objects.select_related('profesor__perfil').get(id=laboratorio_id)
                
                # Verificar que sea un laboratorio
                try:
                    grupo_lab = GrupoLaboratorio.objects.get(grupo_curso=grupo_curso)
                except GrupoLaboratorio.DoesNotExist:
                    return JsonResponse({'ok': False, 'msg': 'No es un laboratorio válido'})
                
                # Obtenemos sus bloques horarios
                bloques = BloqueHorario.objects.filter(grupo_curso=grupo_curso).select_related('aula')
                horarios = [{
                    'dia': b.dia,
                    'inicio': b.horaInicio.strftime('%H:%M:%S') if b.horaInicio else '',
                    'fin': b.horaFin.strftime('%H:%M:%S') if b.horaFin else '',
                    'aula_id': b.aula.id if b.aula else ''
                } for b in bloques]

                data = {
                    'id': grupo_curso.id,
                    'profesor_id': grupo_curso.profesor.perfil.id if grupo_curso.profesor else '',
                    'capacidad': grupo_curso.capacidad,
                    'horarios': horarios
                }
                return JsonResponse({'ok': True, 'data': data})
            except GrupoCurso.DoesNotExist:
                return JsonResponse({'ok': False, 'msg': 'Laboratorio no encontrado'})
            except Exception as e:
                return JsonResponse({'ok': False, 'msg': f'Error al obtener detalle: {str(e)}'})

        return JsonResponse({'ok': False, 'msg': 'Acción desconocida'})

    # ======================================================================
    # 2. MANEJO DE SOLICITUDES POST (CRUD)
    # ======================================================================
    if request.method == 'POST':
        accion = request.POST.get('accion')
        
        try:
            with transaction.atomic():
                
                # --- A. CREAR LABORATORIO ---
                if accion == 'crear_laboratorio':
                    curso_id = request.POST.get('curso_id')
                    grupo_teoria_id = request.POST.get('grupo_teoria_id')  # Grupo teoría asociado
                    letra_grupo = request.POST.get('grupo', '').upper()  # Letra para el laboratorio
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    print(f"DEBUG - horarios_json recibido: {horarios_json}")
                    
                    # Verificar que el grupo de teoría exista
                    try:
                        grupo_teoria = GrupoTeoria.objects.get(grupo_curso_id=grupo_teoria_id)
                    except GrupoTeoria.DoesNotExist:
                        messages.error(request, f"El grupo de teoría {grupo_teoria_id} no existe.")
                        return redirect('usuarios:gestion_laboratorios_admin')
                    
                    # Generación del ID Compuesto: "L" + CURSOID + LETRA
                    nuevo_id = f"L{curso_id}{letra_grupo}"
                    
                    if GrupoCurso.objects.filter(id=nuevo_id).exists():
                        messages.error(request, f"El Laboratorio {letra_grupo} para este curso ya existe (ID: {nuevo_id}).")
                    else:
                        try:
                            # Validar horarios_json
                            if not horarios_json or horarios_json.strip() == '':
                                raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                            
                            # 1. Parsear JSON
                            horarios = json.loads(horarios_json)
                            
                            # Validar que haya al menos un horario
                            if not isinstance(horarios, list) or len(horarios) == 0:
                                raise ValueError("Debe agregar al menos un bloque de horario.")
                            
                            # 2. Iniciar Transacción Atómica
                            with transaction.atomic():
                                # Validación de existencia de objetos FK y conversión de capacidad
                                profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                                curso = Curso.objects.get(id=curso_id)
                                capacidad_int = int(capacidad)
                                
                                # Verificar que el profesor sea de laboratorio
                                if profesor and not profesor.es_lab:
                                    raise ValueError(f"El profesor {profesor.perfil.nombre} no está habilitado para laboratorios.")
                                
                                # 3. Validar cada horario
                                for i, h in enumerate(horarios, 1):
                                    try:
                                        aula_obj = Aula.objects.get(id=h['aula_id'])
                                    except ObjectDoesNotExist:
                                        raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                    
                                    # Validar que el aula sea de tipo laboratorio
                                    if aula_obj.tipo != 'LABORATORIO':
                                        raise ValueError(f"El aula {aula_obj.id} no es un laboratorio. Solo se pueden usar aulas tipo LABORATORIO.")
                                    
                                    # Validar campos requeridos
                                    if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                        raise ValueError(f"El horario {i} tiene campos incompletos.")
                                    
                                    # Convertir tiempos
                                    try:
                                        inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                        fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                    except ValueError:
                                        raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                    
                                    # Validar que inicio < fin
                                    if inicio_time >= fin_time:
                                        raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                    
                                    # 3.1 Verificar cruce con horarios existentes en la misma aula
                                    horarios_cruzados_aula = BloqueHorario.objects.filter(
                                        aula=aula_obj,
                                        dia=h['dia']
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_cruzados_aula.exists():
                                        conflicto = horarios_cruzados_aula.first()
                                        curso_conflicto = conflicto.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto.grupo_curso.grupo
                                        raise ValueError(
                                            f"Conflicto de horario en laboratorio {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                            f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                        )
                                    
                                    # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                    if profesor:
                                        # Buscar horarios donde el docente ya tenga clases asignadas
                                        horarios_docente = BloqueHorario.objects.filter(
                                            grupo_curso__profesor=profesor,
                                            dia=h['dia']
                                        ).exclude(
                                            Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                        )
                                        
                                        if horarios_docente.exists():
                                            conflicto_docente = horarios_docente.first()
                                            curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                            grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                            aula_conflicto = conflicto_docente.aula.id
                                            raise ValueError(
                                                f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                                f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                            )

                                # 4. Crear el GrupoCurso base
                                nuevo_grupo = GrupoCurso.objects.create(
                                    id=nuevo_id,
                                    curso=curso,
                                    profesor=profesor,
                                    grupo=letra_grupo,
                                    capacidad=capacidad_int
                                )
                                
                                # 5. Crear la especialización GrupoLaboratorio
                                GrupoLaboratorio.objects.create(grupo_curso=nuevo_grupo)
                                
                                # 6. Crear los Bloques de Horario
                                for h in horarios:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                    BloqueHorario.objects.create(
                                        dia=h['dia'],
                                        horaInicio=h['inicio'],
                                        horaFin=h['fin'],
                                        grupo_curso=nuevo_grupo,
                                        aula=aula_obj
                                    )
                            
                            messages.success(request, f"Laboratorio {letra_grupo} creado exitosamente.")

                        except ObjectDoesNotExist as e:
                            messages.error(request, f"Error de referencia: {str(e)}")
                        except ValueError as e:
                            messages.error(request, f"Error en los datos: {str(e)}")
                        except json.JSONDecodeError as e:
                            messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                        except Exception as e:
                            messages.error(request, f"Error inesperado al crear el laboratorio: {str(e)}")

                # --- B. EDITAR LABORATORIO ---
                elif accion == 'editar_laboratorio':
                    laboratorio_id = request.POST.get('laboratorio_id')  # ID completo (Ej: INF101AL)
                    profesor_id = request.POST.get('profesor_id')
                    capacidad = request.POST.get('capacidad')
                    horarios_json = request.POST.get('horarios_json', '[]')
                    
                    # 1. Recuperar GrupoCurso o lanzar 404 si no existe
                    grupo_curso = get_object_or_404(GrupoCurso, id=laboratorio_id)
                    
                    # Verificar que sea un laboratorio
                    try:
                        grupo_lab = GrupoLaboratorio.objects.get(grupo_curso=grupo_curso)
                    except GrupoLaboratorio.DoesNotExist:
                        messages.error(request, "El grupo seleccionado no es un laboratorio.")
                        return redirect('usuarios:gestion_laboratorios_admin')
                    
                    try:
                        # Validar horarios_json
                        if not horarios_json or horarios_json.strip() == '':
                            raise ValueError("No se recibieron datos de horarios. Por favor agregue al menos un horario.")
                        
                        # Parsear JSON
                        horarios = json.loads(horarios_json)
                        
                        # Validar que haya al menos un horario
                        if not isinstance(horarios, list) or len(horarios) == 0:
                            raise ValueError("Debe agregar al menos un bloque de horario.")
                        
                        # 2. Iniciar Transacción Atómica
                        with transaction.atomic():
                            # Validación de existencia de objetos FK y conversión de capacidad
                            profesor = Profesor.objects.get(perfil__id=profesor_id) if profesor_id else None
                            capacidad_int = int(capacidad)
                            
                            # Verificar que el profesor sea de laboratorio
                            if profesor and not profesor.es_lab:
                                raise ValueError(f"El profesor {profesor.perfil.nombre} no está habilitado para laboratorios.")
                            
                            # 3. Validar cada horario antes de eliminar los existentes
                            for i, h in enumerate(horarios, 1):
                                try:
                                    aula_obj = Aula.objects.get(id=h['aula_id'])
                                except ObjectDoesNotExist:
                                    raise ValueError(f"El aula con ID '{h['aula_id']}' no existe.")
                                
                                # Validar que el aula sea de tipo laboratorio
                                if aula_obj.tipo != 'LABORATORIO':
                                    raise ValueError(f"El aula {aula_obj.id} no es un laboratorio. Solo se pueden usar aulas tipo LABORATORIO.")
                                
                                # Validar campos requeridos
                                if not h.get('dia') or not h.get('inicio') or not h.get('fin'):
                                    raise ValueError(f"El horario {i} tiene campos incompletos.")
                                
                                # Convertir tiempos
                                try:
                                    inicio_time = datetime.strptime(h['inicio'], '%H:%M:%S').time()
                                    fin_time = datetime.strptime(h['fin'], '%H:%M:%S').time()
                                except ValueError:
                                    raise ValueError(f"Formato de hora inválido en horario {i}. Use HH:MM:SS")
                                
                                # Validar que inicio < fin
                                if inicio_time >= fin_time:
                                    raise ValueError(f"El horario {i} tiene hora de inicio ({h['inicio']}) mayor o igual a la hora de fin ({h['fin']}).")
                                
                                # 3.1 Verificar cruce con horarios existentes en la misma aula
                                # Excluir los horarios del laboratorio actual que estamos editando
                                horarios_cruzados_aula = BloqueHorario.objects.filter(
                                    aula=aula_obj,
                                    dia=h['dia']
                                ).exclude(
                                    grupo_curso=grupo_curso  # Excluir los horarios del laboratorio actual
                                ).exclude(
                                    Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                )
                                
                                if horarios_cruzados_aula.exists():
                                    conflicto = horarios_cruzados_aula.first()
                                    curso_conflicto = conflicto.grupo_curso.curso.nombre
                                    grupo_conflicto = conflicto.grupo_curso.grupo
                                    raise ValueError(
                                        f"Conflicto de horario en laboratorio {aula_obj.id} el día {h['dia']} de {h['inicio']} a {h['fin']}. "
                                        f"Ya existe la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en ese horario."
                                    )
                                
                                # 3.2 Verificar disponibilidad del docente (si se asignó un profesor)
                                if profesor:
                                    # Buscar horarios donde el docente ya tenga clases asignadas
                                    # Excluir los horarios del laboratorio actual que estamos editando
                                    horarios_docente = BloqueHorario.objects.filter(
                                        grupo_curso__profesor=profesor,
                                        dia=h['dia']
                                    ).exclude(
                                        grupo_curso=grupo_curso  # Excluir los horarios del laboratorio actual
                                    ).exclude(
                                        Q(horaFin__lte=inicio_time) | Q(horaInicio__gte=fin_time)
                                    )
                                    
                                    if horarios_docente.exists():
                                        conflicto_docente = horarios_docente.first()
                                        curso_conflicto = conflicto_docente.grupo_curso.curso.nombre
                                        grupo_conflicto = conflicto_docente.grupo_curso.grupo
                                        aula_conflicto = conflicto_docente.aula.id
                                        raise ValueError(
                                            f"El profesor {profesor.perfil.nombre} ya tiene clase asignada el día {h['dia']} entre {h['inicio']} a {h['fin']}. "
                                            f"Está asignado a la clase '{curso_conflicto}' (Grupo {grupo_conflicto}) en el aula {aula_conflicto} en ese horario."
                                        )

                            # 4. Actualizar datos básicos
                            grupo_curso.profesor = profesor
                            grupo_curso.capacidad = capacidad_int
                            grupo_curso.save()
                            
                            # 5. Actualizar horarios: Estrategia de reemplazo total (Borrar y crear)
                            BloqueHorario.objects.filter(grupo_curso=grupo_curso).delete()
                            
                            for h in horarios:
                                aula_obj = Aula.objects.get(id=h['aula_id'])
                                BloqueHorario.objects.create(
                                    dia=h['dia'],
                                    horaInicio=h['inicio'],
                                    horaFin=h['fin'],
                                    grupo_curso=grupo_curso,
                                    aula=aula_obj
                                )
                        
                        messages.success(request, "Laboratorio actualizado correctamente.")
                    
                    except ObjectDoesNotExist as e:
                        messages.error(request, f"Error de referencia: {str(e)}")
                    except ValueError as e:
                        messages.error(request, f"Error en los datos proporcionados: {str(e)}")
                    except json.JSONDecodeError as e:
                        messages.error(request, f"Error en el formato de los horarios (JSON inválido): {str(e)}")
                    except Exception as e:
                        messages.error(request, f"Error inesperado al actualizar el laboratorio: {str(e)}")

                # --- C. ELIMINAR LABORATORIO ---
                elif accion == 'eliminar_laboratorio':
                    laboratorio_id = request.POST.get('laboratorio_id')
                    # Verificar que sea un laboratorio
                    try:
                        grupo_lab = GrupoLaboratorio.objects.get(grupo_curso_id=laboratorio_id)
                        # Al eliminar GrupoCurso, el CASCADE elimina GrupoLaboratorio y BloqueHorario
                        GrupoCurso.objects.filter(id=laboratorio_id).delete()
                        messages.success(request, "Laboratorio eliminado correctamente.")
                    except GrupoLaboratorio.DoesNotExist:
                        messages.error(request, "El grupo seleccionado no es un laboratorio.")

        except IntegrityError as e:
            messages.error(request, f"Error de integridad en la base de datos: {str(e)}")
        except Exception as e:
            messages.error(request, f"Ocurrió un error inesperado: {str(e)}")
            
        return redirect('usuarios:gestion_laboratorios_admin')

    # ======================================================================
    # 3. LÓGICA GET (RENDERIZADO DE LA PÁGINA)
    # ======================================================================
    
    # Obtener cursos con laboratorios y sus conteos
    cursos = Curso.objects.filter(
        grupocurso__grupolaboratorio__isnull=False
    ).distinct().annotate(
        num_laboratorios=Count('grupocurso__grupolaboratorio', distinct=True),
        num_matriculados=Count('grupocurso__grupolaboratorio__matriculalaboratorio', 
                               filter=Q(grupocurso__grupolaboratorio__isnull=False), 
                               distinct=True)
    ).order_by('id')

    # Listas auxiliares
    profesores_lab = Profesor.objects.filter(es_lab=True).select_related('perfil').order_by('perfil__nombre')
    aulas_lab = Aula.objects.filter(tipo='LABORATORIO').order_by('id')

    contexto = {
        'perfil': perfil_obj.perfil, 
        'titulo': 'Gestión de Laboratorios',
        'cursos': cursos,
        'profesores': profesores_lab,
        'aulas': aulas_lab
    }
    return render(request, 'usuarios/admin/gestion_laboratorios_admin.html', contexto)

def registro_estudiantes_admin(request):
    # ===========================
    #   SUBIR CSV
    # ===========================
    if request.method == "POST" and "subir_csv" in request.POST:
        try:
            file = request.FILES.get("csv_estudiantes")

            if not file:
                messages.error(request, "Debe seleccionar un archivo CSV.")
                return redirect("usuarios:registro_estudiantes_admin")

            # Convertimos a texto UTF-8
            decoded = file.read().decode("utf-8")
            reader = csv.reader(decoded.splitlines())

            # Formato esperado (sin encabezados):
            # cui, nombre, email, password
            for row in reader:
                if len(row) < 4:
                    continue

                cui = row[0].strip()
                nombre = row[1].strip()
                email = row[2].strip() if row[2].strip() else None
                password = row[3].strip()

                if not (cui and nombre and password):
                    continue  # Campos obligatorios

                # Crear o actualizar Perfil
                perfil, created = Perfil.objects.get_or_create(
                    id=cui,
                    defaults={
                        "nombre": nombre,
                        "email": email,
                        "password": password,
                        "rol": "ESTUDIANTE",
                        "estadoCuenta": True
                    }
                )

                # Si el perfil ya existe, actualizar nombre/email/password si cambian
                if not created:
                    perfil.nombre = nombre
                    perfil.email = email
                    perfil.password = password
                    perfil.rol = "ESTUDIANTE"
                    perfil.estadoCuenta = True
                    perfil.save()

                # Crear estudiante si no existe
                Estudiante.objects.get_or_create(perfil=perfil)

            messages.success(request, "CSV procesado correctamente.")

        except Exception as e:
            messages.error(request, f"Error procesando CSV: {str(e)}")

        return redirect("usuarios:registro_estudiantes_admin")

    # ===========================
    #   CREAR ESTUDIANTE MANUAL
    # ===========================
    if request.method == "POST" and "crear_estudiante" in request.POST:
        cui = request.POST.get("cui")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        password = request.POST.get("password")

        # Validación SOLO de los campos reales
        if not (cui and nombre and password):
            messages.error(request, "CUI, Nombre y Contraseña son obligatorios.")
            return redirect("usuarios:registro_estudiantes_admin")

        # Crear Perfil
        perfil = Perfil.objects.create(
            id=cui,
            nombre=nombre,
            email=email if email else None,
            password=password,
            rol="ESTUDIANTE",
            estadoCuenta=True
        )

        # Crear Estudiante
        Estudiante.objects.create(perfil=perfil)

        messages.success(request, "Estudiante creado correctamente.")
        return redirect("usuarios:registro_estudiantes_admin")

    # ===========================
    #   EDITAR ESTUDIANTE
    # ===========================
    if request.method == "POST" and "editar_estudiante" in request.POST:
        est_id = request.POST.get("estudiante_id")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        estado = request.POST.get("estadoCuenta")
        grupo_id = request.POST.get("grupo_curso_id")

        perfil = Perfil.objects.get(id=est_id)

        # Actualizar datos del perfil
        perfil.nombre = nombre
        perfil.email = email if email else None
        perfil.estadoCuenta = (estado == "True")
        perfil.save()

        # Asignar curso si corresponde
        if grupo_id:
            estudiante = Estudiante.objects.get(perfil_id=est_id)
            grupo = GrupoCurso.objects.get(id=grupo_id)

            Matricula.objects.get_or_create(
                estudiante=estudiante,
                grupo_curso=grupo,
                defaults={"estado": True}
            )

        messages.success(request, "Estudiante actualizado.")
        return redirect("usuarios:registro_estudiantes_admin")
    
    # =============================
    #   ELIMINAR ESTUDIANTE
    # =============================
    if request.method == "POST" and "eliminar_estudiante" in request.POST:
        estudiante_id = request.POST.get("estudiante_id")

        try:
            perfil = Perfil.objects.get(id=estudiante_id)

            # Al eliminar el perfil, se elimina automáticamente el Estudiante
            # por el OnDelete.CASCADE
            perfil.delete()

            messages.success(request, "La cuenta del estudiante fue eliminada correctamente.")
        except Perfil.DoesNotExist:
            messages.error(request, "El estudiante no existe.")

        return redirect("usuarios:registro_estudiantes_admin")

    # ===========================
    #     MATRICULAR ESTUDIANTE
    # ===========================
    if request.method == "POST" and "asignar_curso" in request.POST:

        estudiante_id = request.POST.get("estudiante_id")
        grupo_curso_id = request.POST.get("grupo_curso_id")

        try:
            estudiante = Estudiante.objects.get(pk=estudiante_id)
            grupo_curso = GrupoCurso.objects.get(pk=grupo_curso_id)

            # Crear la matrícula
            Matricula.objects.create(
                estudiante=estudiante,
                grupo_curso=grupo_curso,
                estado=True  # Activo por defecto
            )

            messages.success(request, "Curso asignado correctamente.")

        except Estudiante.DoesNotExist:
            messages.error(request, "El estudiante no existe.")

        except GrupoCurso.DoesNotExist:
            messages.error(request, "El grupo del curso no existe.")

        except:
            messages.error(request, "Este estudiante ya está matriculado en ese grupo.")

        return redirect("usuarios:registro_estudiantes_admin")

    # Activar / Desactivar cuenta
    if "toggle_estado" in request.POST:
        perfil = Perfil.objects.get(id=request.POST["estudiante_id"])
        perfil.estadoCuenta = not perfil.estadoCuenta
        perfil.save()
        messages.success(request, "Estado actualizado.")
        return redirect("usuarios:registro_estudiantes_admin")

    # ===========================
    #   LISTAR ESTUDIANTES
    # ===========================
    estudiantes = Estudiante.objects.select_related("perfil").all()
    grupos = GrupoCurso.objects.filter(grupoteoria__isnull=False).select_related('curso', 'profesor__perfil').all().order_by("curso__nombre")

    context = {
        "estudiantes": estudiantes,
        "grupos": grupos,
    }
    return render(request, "usuarios/admin/registro_estudiantes_admin.html", context)


def detalle_estudiante_admin(request):
    perfil_obj, response = check_administrador_auth(request)
    if response:
        return response

    cui = request.GET.get('cui') or (
        request.resolver_match.kwargs.get('cui')
        if request.resolver_match else None
    )
    if not cui:
        messages.error(request, "No se indicó CUI del estudiante.")
        return redirect(reverse('usuarios:registro_estudiantes_admin'))

    # Obtener estudiante
    perfil_estudiante = get_object_or_404(Perfil, pk=cui)
    estudiante = get_object_or_404(Estudiante, perfil=perfil_estudiante)

    # Matriculas
    matriculas_qs = Matricula.objects.filter(
        estudiante=estudiante
    ).select_related(
        'grupo_curso__curso',
        'grupo_curso__profesor__perfil'
    ).order_by('grupo_curso__curso__nombre')

    # Laboratorios inscritos
    labs_qs = (
        MatriculaLaboratorio.objects.filter(estudiante=estudiante)
        .select_related(
            "laboratorio__grupo_curso__curso",
            "laboratorio__grupo_curso__profesor__perfil"
        )
    )

    laboratorios = []
    for mlab in labs_qs:
        glab = mlab.laboratorio          # GrupoLaboratorio
        gcurso = glab.grupo_curso        # GrupoCurso asociado

        laboratorios.append({
            "id": glab.pk,
            "curso": gcurso.curso.nombre,
            "curso_id": gcurso.curso.id,
            "profesor": gcurso.profesor.perfil.nombre if gcurso.profesor else "-",
            "grupo": gcurso.grupo,
        })

    matriculas = []
    grupo_ids = []

    for m in matriculas_qs:
        matriculas.append({
            'id': m.id,
            'grupo': m.grupo_curso,
            'estado': m.estado,
        })
        grupo_ids.append(m.grupo_curso.id)

    # Horarios
    horarios_map = {}
    if grupo_ids:
        bloques = BloqueHorario.objects.filter(
            grupo_curso__id__in=grupo_ids
        ).select_related('aula', 'grupo_curso')

        for b in bloques:
            gid = b.grupo_curso.id
            horarios_map.setdefault(gid, []).append({
                'dia': b.dia,
                'horaInicio': b.horaInicio.strftime("%H:%M"),
                'horaFin': b.horaFin.strftime("%H:%M"),
                'aula': getattr(b.aula, 'nombre', getattr(b.aula, 'id', '-'))
            })

    # Cargar NOTAS desde la tabla Matricula
    notas_por_curso = {}
    for m in matriculas_qs:
        gid = m.grupo_curso.id
        notas_por_curso[gid] = {
            'EC1': m.EC1,
            'EP1': m.EP1,
            'EC2': m.EC2,
            'EP2': m.EP2,
            'EC3': m.EC3,
            'EP3': m.EP3,
        }

    # Asistencias
    asistencias_qs = RegistroAsistenciaDetalle.objects.filter(
        estudiante=estudiante
    ).select_related(
        'registro_asistencia__grupo_curso__curso'
    ).order_by('-registro_asistencia__fechaClase')[:30]

    asistencias = []
    total_presentes = 0
    total_faltas = 0

    for d in asistencias_qs:
        fecha = d.registro_asistencia.fechaClase.strftime("%Y-%m-%d")
        grupo = d.registro_asistencia.grupo_curso
        curso_nombre = grupo.curso.nombre if grupo and grupo.curso else ''
        estado = d.estado

        if estado == 'PRESENTE':
            total_presentes += 1
        else:
            total_faltas += 1

        asistencias.append({
            'fecha': fecha,
            'grupo_id': grupo.id if grupo else '',
            'curso_nombre': curso_nombre,
            'estado': estado
        })

    resumen = {
        'total_matriculas': matriculas_qs.count(),
        'total_presentes': total_presentes,
        'total_faltas': total_faltas
    }

    contexto = {
        'perfil': perfil_obj,
        'titulo': f'Perfil Estudiante - {perfil_estudiante.nombre}',
        'perfil_estudiante': perfil_estudiante,
        'estudiante': estudiante,
        'matriculas': [{'grupo': m['grupo'], 'estado': m['estado']} for m in matriculas],
        'horarios_by_group': {'mat_map': horarios_map},
        'notas_por_curso': notas_por_curso,
        'laboratorios': laboratorios,
        'asistencias': asistencias,
        'resumen': resumen,
    }

    return render(request, 'usuarios/admin/detalle_estudiante_admin.html', contexto)

def detalle_profesor_admin(request):
    perfil_obj, response = check_administrador_auth(request)
    if response:
        return response

    codigo = request.GET.get("codigo") or (
        request.resolver_match.kwargs.get("codigo")
        if request.resolver_match else None
    )
    if not codigo:
        messages.error(request, "No se indicó Código del profesor.")
        return redirect(reverse("usuarios:registro_profesores_admin"))

    # ================================
    #   1. Obtener profesor
    # ================================
    perfil_prof = get_object_or_404(Perfil, pk=codigo)
    profesor = get_object_or_404(Profesor, perfil=perfil_prof)

    # ================================
    #   2. Obtener sus grupos asignados
    # ================================
    grupos_qs = (
        GrupoCurso.objects.filter(profesor=profesor)
        .select_related("curso", "profesor__perfil")
        .order_by("curso__nombre")
    )

    # Determinar qué grupos son teoría
    grupos_teoria_ids = set(
        GrupoTeoria.objects.filter(grupo_curso__in=grupos_qs)
        .values_list("grupo_curso_id", flat=True)
    )

    # ================================
    #   3. Horarios por cada grupo
    # ================================
    horarios_map = {}
    if grupos_qs:
        bloques = (
            BloqueHorario.objects.filter(grupo_curso__in=grupos_qs)
            .select_related("aula", "grupo_curso")
        )
        for b in bloques:
            gid = b.grupo_curso.id
            horarios_map.setdefault(gid, []).append({
                "dia": b.dia,
                "horaInicio": b.horaInicio.strftime("%H:%M"),
                "horaFin": b.horaFin.strftime("%H:%M"),
                "aula": getattr(b.aula, "nombre", getattr(b.aula, "id", "-")),
            })

    # ================================
    #   4. Temas (solo para teoría)
    # ================================
    temas_map = {}
    teoria_ids = list(grupos_teoria_ids)

    if teoria_ids:
        temas_qs = (
            TemaCurso.objects.filter(grupo_teoria_id__in=teoria_ids)
            .order_by("orden")
        )
        for t in temas_qs:
            temas_map.setdefault(t.grupo_teoria_id, []).append({
                "orden": t.orden,
                "nombre": t.nombre,
                "fecha": t.fecha.strftime("%Y-%m-%d") if t.fecha else "-",
                "completado": t.completado,
            })

    # ================================
    #   5. Reservas del profesor
    # ================================
    reservas_qs = (
        Reserva.objects
        .filter(profesor=profesor)
        .select_related("aula")
        .order_by("-fecha_reserva")
    )

    reservas = []

    for r in reservas_qs:
        reservas.append({
            "fecha_reserva": r.fecha_reserva.strftime("%Y-%m-%d"),
            "hora_inicio": r.hora_inicio.strftime("%H:%M"),
            "hora_fin": r.hora_fin.strftime("%H:%M"),
            "aula_id": r.aula.id,
            "aula_tipo": r.aula.get_tipo_display(),
        })

    # ================================
    #   6. Asistencias tomadas
    # ================================
    grupos_ids = GrupoCurso.objects.filter(profesor=profesor).values_list('id', flat=True)

    asistencias_qs = RegistroAsistencia.objects.filter(
        grupo_curso__id__in=grupos_ids
    ).select_related('grupo_curso__curso').order_by('-fechaClase')[:30]

    asistencias = [{
        'fechaClase': r.fechaClase.strftime("%Y-%m-%d"),
        'horaInicioVentana': r.horaInicioVentana.strftime("%H:%M"),
        'grupo_curso': r.grupo_curso,
        'ipProfesor': r.ipProfesor
    } for r in asistencias_qs]

    # ================================
    #   7. Resumen
    # ================================
    resumen = {
        "total_cursos": grupos_qs.count(),
        "total_reservas": reservas_qs.count(),
        "total_asistencias": asistencias_qs.count(),
    }

    # ================================
    #   8. Contexto final
    # ================================
    contexto = {
        "perfil": perfil_obj,
        "profesor": profesor,
        "grupos": grupos_qs,
        "grupos_teoria_ids": grupos_teoria_ids,
        "horarios": horarios_map,
        "temas": temas_map,
        "reservas": reservas,
        "asistencias": asistencias,
        "resumen": resumen,
        "titulo": f"Perfil Profesor - {profesor.perfil.nombre}",
    }

    return render(request, "usuarios/admin/detalle_profesor_admin.html", contexto)

def registro_profesores_admin(request):
    # ===========================
    #   SUBIR CSV
    # ===========================
    if request.method == "POST" and "subir_csv" in request.POST:
        try:
            file = request.FILES.get("csv_profesores")

            if not file:
                messages.error(request, "Debe seleccionar un archivo CSV.")
                return redirect("usuarios:registro_profesores_admin")

            decoded = file.read().decode("utf-8")
            reader = csv.reader(decoded.splitlines())

            # Formato esperado:
            # cui, nombre, email, password
            for row in reader:
                if len(row) < 4:
                    continue

                codigo = row[0].strip()
                nombre = row[1].strip()
                email = row[2].strip() or None
                password = row[3].strip()

                if not (codigo and nombre and password):
                    continue

                perfil, created = Perfil.objects.get_or_create(
                    id=codigo,
                    defaults={
                        "nombre": nombre,
                        "email": email,
                        "password": password,
                        "rol": "PROFESOR",
                        "estadoCuenta": True
                    }
                )

                if not created:
                    perfil.nombre = nombre
                    perfil.email = email
                    perfil.password = password
                    perfil.rol = "PROFESOR"
                    perfil.estadoCuenta = True
                    perfil.save()

                Profesor.objects.get_or_create(perfil=perfil)

            messages.success(request, "CSV procesado correctamente.")

        except Exception as e:
            messages.error(request, f"Error procesando CSV: {str(e)}")

        return redirect("usuarios:registro_profesores_admin")

    # ===========================
    #   CREAR PROFESOR MANUAL
    # ===========================
    if request.method == "POST" and "crear_profesor" in request.POST:

        codigo = request.POST.get("cui")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        password = request.POST.get("password")

        if not (codigo and nombre and password):
            messages.error(request, "Código, Nombre y Contraseña son obligatorios.")
            return redirect("usuarios:registro_profesores_admin")

        perfil = Perfil.objects.create(
            id=codigo,
            nombre=nombre,
            email=email or None,
            password=password,
            rol="PROFESOR",
            estadoCuenta=True
        )

        Profesor.objects.create(perfil=perfil)

        messages.success(request, "Profesor creado correctamente.")
        return redirect("usuarios:registro_profesores_admin")

    # ===========================
    #   EDITAR PROFESOR
    # ===========================
    if request.method == "POST" and "editar_profesor" in request.POST:

        profesor_id = request.POST.get("profesor_id")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        estado = request.POST.get("estadoCuenta")
        asignar_grupo_id = request.POST.get("grupo_curso_id")
        tipo_clase = request.POST.get("tipo_clase")

        profe = Profesor.objects.get(perfil__id=profesor_id)
        if tipo_clase == "ambos":
            profe.es_teoria = True
            profe.es_lab = True
        elif tipo_clase == "teoria":
            profe.es_teoria = True
            profe.es_lab = False
        elif tipo_clase == "laboratorio":
            profe.es_teoria = False
            profe.es_lab = True
        
        profe.save()

        perfil = Perfil.objects.get(id=profesor_id)

        perfil.nombre = nombre
        perfil.email = email or None
        perfil.estadoCuenta = (estado == "True")
        perfil.save()

        if asignar_grupo_id:
            grupo = GrupoCurso.objects.get(id=asignar_grupo_id)
            grupo.profesor = profe
            grupo.save()

        messages.success(request, "Profesor actualizado correctamente.")
        return redirect("usuarios:registro_profesores_admin")

    # ===========================
    #   ELIMINAR PROFESOR
    # ===========================
    if request.method == "POST" and "eliminar_profesor" in request.POST:

        profesor_id = request.POST.get("profesor_id")

        try:
            perfil = Perfil.objects.get(id=profesor_id)
            perfil.delete()
            messages.success(request, "La cuenta del profesor fue eliminada.")
        except Perfil.DoesNotExist:
            messages.error(request, "El profesor no existe.")

        return redirect("usuarios:registro_profesores_admin")

    # ===========================
    #   ACTIVAR / DESACTIVAR
    # ===========================
    if "toggle_estado" in request.POST:

        perfil = Perfil.objects.get(id=request.POST["profesor_id"])
        perfil.estadoCuenta = not perfil.estadoCuenta
        perfil.save()
        messages.success(request, "Estado actualizado.")
        return redirect("usuarios:registro_profesores_admin")

    # ===========================
    #   LISTAR PROFESORES
    # ===========================
    profesores = Profesor.objects.select_related("perfil").all().order_by("perfil__nombre")
    grupos_disponibles = GrupoCurso.objects.filter(profesor__isnull=True).filter(Q(grupoteoria__isnull=False) | Q(grupolaboratorio__isnull=False)).select_related("curso").order_by("curso__nombre", "grupo")

    return render(request, "usuarios/admin/registro_profesores_admin.html", {
        "profesores": profesores,   
        "grupos_disponibles_profesor": grupos_disponibles,
    })

def registro_secretarias(request):
    # ===========================
    #   SUBIR CSV
    # ===========================
    if request.method == "POST" and "subir_csv" in request.POST:
        try:
            file = request.FILES.get("csv_secretarias")

            if not file:
                messages.error(request, "Debe seleccionar un archivo CSV.")
                return redirect("usuarios:registro_secretarias")

            # Convertimos a texto UTF-8
            decoded = file.read().decode("utf-8")
            reader = csv.reader(decoded.splitlines())

            # Formato esperado (sin encabezados):
            # id, nombre, email, password
            for row in reader:
                if len(row) < 4:
                    continue

                secretaria_id = row[0].strip()
                nombre = row[1].strip()
                email = row[2].strip() if row[2].strip() else None
                password = row[3].strip()

                if not (secretaria_id and nombre and password):
                    continue  # Campos obligatorios

                # Crear o actualizar Perfil
                perfil, created = Perfil.objects.get_or_create(
                    id=secretaria_id,
                    defaults={
                        "nombre": nombre,
                        "email": email,
                        "password": password,
                        "rol": "SECRETARIA",
                        "estadoCuenta": True
                    }
                )

                # Si el perfil ya existe, actualizar nombre/email/password si cambian
                if not created:
                    perfil.nombre = nombre
                    perfil.email = email
                    perfil.password = password
                    perfil.rol = "SECRETARIA"
                    perfil.estadoCuenta = True
                    perfil.save()

                # Crear secretaria si no existe
                Secretaria.objects.get_or_create(perfil=perfil)

            messages.success(request, "CSV procesado correctamente.")

        except Exception as e:
            messages.error(request, f"Error procesando CSV: {str(e)}")

        return redirect("usuarios:registro_secretarias")

    # ===========================
    #   CREAR SECRETARIA MANUAL
    # ===========================
    if request.method == "POST" and "crear_secretaria" in request.POST:
        secretaria_id = request.POST.get("secretaria_id")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        password = request.POST.get("password")

        # Validación SOLO de los campos reales
        if not (secretaria_id and nombre and password):
            messages.error(request, "ID, Nombre y Contraseña son obligatorios.")
            return redirect("usuarios:registro_secretarias")

        # Verificar si ya existe un perfil con este ID
        if Perfil.objects.filter(id=secretaria_id).exists():
            messages.error(request, "Ya existe un usuario con este ID.")
            return redirect("usuarios:registro_secretarias")

        # Verificar email único si se proporciona
        if email and Perfil.objects.filter(email=email).exists():
            messages.error(request, "El email ya está registrado.")
            return redirect("usuarios:registro_secretarias")

        # Crear Perfil
        perfil = Perfil.objects.create(
            id=secretaria_id,
            nombre=nombre,
            email=email if email else None,
            password=password,
            rol="SECRETARIA",
            estadoCuenta=True
        )

        # Crear Secretaria
        Secretaria.objects.create(perfil=perfil)

        messages.success(request, "Secretaria creada correctamente.")
        return redirect("usuarios:registro_secretarias")

    # ===========================
    #   EDITAR SECRETARIA
    # ===========================
    if request.method == "POST" and "editar_secretaria" in request.POST:
        secretaria_id = request.POST.get("secretaria_id")
        nombre = request.POST.get("nombre")
        email = request.POST.get("email")
        estado = request.POST.get("estadoCuenta")

        try:
            perfil = Perfil.objects.get(id=secretaria_id, rol="SECRETARIA")
            
            # Verificar email único si se cambia
            if email and email != perfil.email:
                if Perfil.objects.filter(email=email).exclude(id=secretaria_id).exists():
                    messages.error(request, "El email ya está registrado por otro usuario.")
                    return redirect("usuarios:registro_secretarias")

            # Actualizar datos del perfil
            perfil.nombre = nombre
            perfil.email = email if email else None
            perfil.estadoCuenta = (estado == "True")
            perfil.save()

            messages.success(request, "Secretaria actualizada correctamente.")
            
        except Perfil.DoesNotExist:
            messages.error(request, "La secretaria no existe.")
        except Exception as e:
            messages.error(request, f"Error al actualizar la secretaria: {str(e)}")

        return redirect("usuarios:registro_secretarias")
    
    # =============================
    #   ELIMINAR SECRETARIA
    # =============================
    if request.method == "POST" and "eliminar_secretaria" in request.POST:
        secretaria_id = request.POST.get("secretaria_id")

        try:
            perfil = Perfil.objects.get(id=secretaria_id, rol="SECRETARIA")

            # Al eliminar el perfil, se elimina automáticamente la Secretaria
            # por el OnDelete.CASCADE
            perfil.delete()

            messages.success(request, "La secretaria fue eliminada correctamente.")
        except Perfil.DoesNotExist:
            messages.error(request, "La secretaria no existe.")
        except Exception as e:
            messages.error(request, f"Error al eliminar la secretaria: {str(e)}")

        return redirect("usuarios:registro_secretarias")

    # =============================
    #   ACTIVAR / DESACTIVAR CUENTA
    # =============================
    if request.method == "POST" and "toggle_estado" in request.POST:
        secretaria_id = request.POST.get("secretaria_id")
        
        if not secretaria_id:
            messages.error(request, "ID de secretaria no proporcionado.")
            return redirect("usuarios:registro_secretarias")
        
        try:
            perfil = Perfil.objects.get(id=secretaria_id, rol="SECRETARIA")
            perfil.estadoCuenta = not perfil.estadoCuenta
            perfil.save()
            
            estado_texto = "activada" if perfil.estadoCuenta else "desactivada"
            messages.success(request, f"Secretaria {estado_texto} correctamente.")
            
        except Perfil.DoesNotExist:
            messages.error(request, "La secretaria no existe.")
        except Exception as e:
            messages.error(request, f"Error al cambiar el estado: {str(e)}")

        return redirect("usuarios:registro_secretarias")

    # ===========================
    #   LISTAR SECRETARIAS
    # ===========================
    secretarias = Secretaria.objects.select_related("perfil").all().order_by("perfil__nombre")

    context = {
        "secretarias": secretarias,
        "titulo": "Gestión de Secretarias"
    }
    return render(request, "usuarios/admin/registro_secretarias.html", context)