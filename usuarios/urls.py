# usuarios/urls.py
from django.urls import path
from . import views

app_name = 'usuarios' 

urlpatterns = [
    # --- Flujo de Autenticación ---
    path('', views.selector_rol, name='selector_rol'),
    path('login/<str:rol_seleccionado>/', views.login_usuario, name='login_usuario'),
    path('logout/', views.logout_usuario, name='logout'), # Nueva función de cierre de sesión
    
    # --- Vistas Específicas de Roles ---
    path('dashboard/estudiante/', views.dashboard_estudiante, name='dashboard_estudiante'),
    path('dashboard/estudiante/mi-cuenta/', views.mi_cuenta, name='mi_cuenta_alumno'),
    path('dashboard/estudiante/matricula-lab/', views.matricula_laboratorio, name='matricula_lab_alumno'),
    path('dashboard/estudiante/mis-cursos/', views.mis_cursos, name='mis_cursos_alumno'),
    path('dashboard/estudiante/mis-horarios/', views.mis_horarios, name='mis_horarios_alumno'),
    path('dashboard/estudiante/mis-notas/', views.mis_notas, name='mis_notas_alumno'),

    path('dashboard/profesor/', views.dashboard_profesor, name='dashboard_profesor'), 
    path('dashboard/profesor/mi-cuenta/', views.mi_cuenta_profesor, name='mi_cuenta_profesor'),
    path('dashboard/profesor/mis-cursos/', views.mis_cursos_profesor, name='mis_cursos_profesor'),
    path('dashboard/profesor/horarios/', views.horarios_profesor, name='horarios_profesor'),
    path('dashboard/profesor/acreditacion/', views.acreditacion, name='acreditacion'),
    path('dashboard/profesor/asistencia/', views.registro_asistencia, name='registro_asistencia'),
    path('dashboard/profesor/reservar-aula/', views.horarios_reserva, name='reservar_aula'),
    path('dashboard/profesor/cancelar-reserva/', views.cancelar_reserva, name='cancelar_reserva'),
    path('dashboard/profesor/subida-notas/', views.subida_notas, name='subida_notas'),

    path('dashboard/secretaria/', views.dashboard_secretaria, name='dashboard_secretaria'), 
    path('dashboard/secretaria/mi-cuenta/', views.mi_cuenta_secretaria, name='mi_cuenta_secretaria'),
    path('dashboard/secretaria/gestion-cursos/', views.gestion_cursos, name='gestion_cursos'),
    path('dashboard/secretaria/ver-horarios/', views.ver_horarios_clases, name='ver_horarios_clases'),
    path('dashboard/secretaria/gestion-laboratorios/', views.gestion_laboratorios, name='gestion_laboratorios'),
    path('dashboard/secretaria/registro-estudiantes/', views.registro_estudiantes, name='registro_estudiantes'),
    path('dashboard/secretaria/detalle-estudiante/', views.detalle_estudiante, name='detalle_estudiante'),
    path('dashboard/secretaria/registro-profesores/', views.registro_profesores, name='registro_profesores'),
    path('dashboard/secretaria/detalle-profesor/', views.detalle_profesor, name='detalle_profesor'),

    path('dashboard/admin/', views.dashboard_admin, name='dashboard_admin'), 
    path('dashboard/admin/mi-cuenta/', views.mi_cuenta_admin, name='mi_cuenta_admin'),
    path('dashboard/admin/gestion-cursos/', views.gestion_cursos_admin, name='gestion_cursos_admin'),
    path('dashboard/admin/ver-horarios/', views.ver_horarios_clases_admin, name='ver_horarios_clases_admin'),
    path('dashboard/admin/gestion-laboratorios/', views.gestion_laboratorios_admin, name='gestion_laboratorios_admin'),
    path('dashboard/admin/registro-estudiantes/', views.registro_estudiantes_admin, name='registro_estudiantes_admin'),
    path('dashboard/admin/detalle-estudiante/', views.detalle_estudiante_admin, name='detalle_estudiante_admin'),
    path('dashboard/admin/registro-profesores/', views.registro_profesores_admin, name='registro_profesores_admin'),
    path('dashboard/admin/detalle-profesor/', views.detalle_profesor_admin, name='detalle_profesor_admin'),
    path('dashboard/admin/registro-secretarias/', views.registro_secretarias, name='registro_secretarias'),
]