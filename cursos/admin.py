from django.contrib import admin
from django.db.models import F, Q
# Importación del modelo Profesor (ASUNCIÓN: La ruta es 'usuarios.models')
from usuarios.models import Profesor 
from .models import (
    Curso, 
    GrupoCurso, 
    GrupoTeoria, 
    GrupoLaboratorio, 
    BloqueHorario, 
    TemaCurso
)

# ----------------------------------------------------------------------
# CLASE DE FILTRO PERSONALIZADO (Integrada en este archivo)
# ----------------------------------------------------------------------
class ProfesorBloqueHorarioFilter(admin.SimpleListFilter):
    """
    Filtro personalizado para listar los Bloques de Horario por Profesor.
    
    Esto evita el error de DisallowedModelAdminLookup al filtrar por 
    la FK que tiene la PK de OneToOneField.
    """
    title = 'Profesor Asignado'
    parameter_name = 'profesor_asignado' 

    def lookups(self, request, model_admin):
        """Genera la lista de todos los profesores que tienen bloques asignados."""
        # 1. Obtenemos los IDs de los profesores únicos que tienen BloqueHorario
        profesor_ids = model_admin.get_queryset(request).values_list(
            'grupo_curso__profesor__perfil_id', 
            flat=True
        ).distinct()
        
        # 2. Filtramos el modelo Profesor y devolvemos la lista (ID, Nombre)
        profesores = Profesor.objects.filter(perfil_id__in=profesor_ids).order_by('perfil__nombre')
        
        # El valor de la tupla (str(p.perfil_id)) será el que se use en la URL y en queryset()
        return [(str(p.perfil_id), str(p)) for p in profesores]

    def queryset(self, request, queryset):
        """Aplica el filtro al queryset basado en la opción seleccionada."""
        if self.value():
            # Filtramos por el perfil_id del profesor seleccionado
            # Esto es lo que rompe la cadena de lookup y soluciona el error.
            return queryset.filter(grupo_curso__profesor__perfil_id=self.value())
        return queryset

# ----------------------------------------------------------------------
# INLINES (Para gestionar modelos relacionados desde el padre)
# ----------------------------------------------------------------------

# 1. Inline para Bloques de Horario (Varios Bloques por GrupoCurso)
class BloqueHorarioInline(admin.TabularInline):
    model = BloqueHorario
    extra = 1 
    verbose_name_plural = 'Horarios Asignados'
    
# 2. Inline para Grupo Teoría (Relación 1:1 con GrupoCurso)
class GrupoTeoriaInline(admin.StackedInline):
    model = GrupoTeoria
    max_num = 1 
    can_delete = False
    verbose_name_plural = 'Es Grupo de Teoría'

# 3. Inline para Grupo Laboratorio (Relación 1:1 con GrupoCurso)
class GrupoLaboratorioInline(admin.StackedInline):
    model = GrupoLaboratorio
    max_num = 1 
    can_delete = False
    verbose_name_plural = 'Es Grupo de Laboratorio'

# ----------------------------------------------------------------------
# MODEL ADMINS
# ----------------------------------------------------------------------

@admin.register(Curso)
class CursoAdmin(admin.ModelAdmin):
    list_display = ('id', 'nombre', 'creditos')
    search_fields = ('id', 'nombre')
    
    fieldsets = (
        ('Información Básica', {
            'fields': ('id', 'nombre', 'creditos', 'silabo_url')
        }),
        ('Porcentajes de Evaluación', {
            'fields': (
                ('porcentajeEC1', 'porcentajeEP1'), 
                ('porcentajeEC2', 'porcentajeEP2'),
                ('porcentajeEC3', 'porcentajeEP3'),
            )
        }),
        ('Evidencia de Acreditación', {
            'fields': (
                ('Fase1notaAlta_url', 'Fase1notaMedia_url', 'Fase1notaBaja_url'),
                ('Fase2notaAlta_url', 'Fase2notaMedia_url', 'Fase2notaBaja_url'),
                ('Fase3notaAlta_url', 'Fase3notaMedia_url', 'Fase3notaBaja_url'),
            ),
            'classes': ('collapse',), 
        }),
    )

@admin.register(GrupoCurso)
class GrupoCursoAdmin(admin.ModelAdmin):
    list_display = ('id', 'curso', 'profesor', 'grupo', 'capacidad')
    list_filter = ('curso', 'profesor', 'grupo')
    raw_id_fields = ('curso', 'profesor')
    search_fields = ('curso__nombre', 'profesor__perfil__nombre')
    
    inlines = [
        GrupoTeoriaInline, 
        GrupoLaboratorioInline, 
        BloqueHorarioInline, 
    ]

@admin.register(TemaCurso)
class TemaCursoAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'orden', 'fecha', 'completado', 'grupo_teoria')
    list_filter = ('grupo_teoria__grupo_curso__curso__nombre', 'completado', 'fecha')
    search_fields = ('nombre',)
    raw_id_fields = ('grupo_teoria',) 

@admin.register(BloqueHorario)
class BloqueHorarioAdmin(admin.ModelAdmin):
    
    def profesor_asignado(self, obj):
        """Retorna el nombre del profesor asignado al GrupoCurso, usando __str__ del modelo Profesor."""
        profesor = obj.grupo_curso.profesor
        return profesor if profesor else 'Sin asignar'
    
    profesor_asignado.short_description = 'Profesor'
    
    # Campos que se muestran en la lista, incluyendo el nuevo método
    list_display = ('grupo_curso', 'profesor_asignado', 'dia', 'horaInicio', 'horaFin', 'aula')
    
    # IMPORTANTE: Usamos la clase de filtro definida justo arriba
    list_filter = (
        'dia', 
        'grupo_curso__curso__nombre', 
        'aula', 
        ProfesorBloqueHorarioFilter, # <--- AÑADIDO EL FILTRO AUTOCONTENIDO
    )
    
    # Aseguramos que el queryset siempre traiga al profesor para evitar accesos lentos a la DB
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        # Hacemos el prefetch de la cadena GrupoCurso -> Profesor
        # Aunque el filtro personalizado ayuda, el select_related es buena práctica de rendimiento.
        qs = qs.select_related('grupo_curso__profesor')
        return qs
    
    # Campos para búsqueda 
    search_fields = (
        'grupo_curso__id', 
        'grupo_curso__curso__nombre', 
        'aula__nombre',
        'grupo_curso__profesor__perfil__nombre' 
    )
    
    raw_id_fields = ('grupo_curso', 'aula')
    
    ordering = ('grupo_curso', 'dia')