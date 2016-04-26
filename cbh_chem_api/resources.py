"""
This is the main module of the new ChemiReg API
It provides the webservices to add, search for and index CBHCompoundBatch objects

"""
from chembl_business_model.models.compounds import CompoundProperties, MoleculeDictionary
from cbh_chembl_model_extension.models import CBHCompoundBatch, generate_uox_id
from tastypie.resources import ALL
from tastypie.resources import ALL_WITH_RELATIONS
from tastypie.resources import ModelResource, Resource
from tastypie.serializers import Serializer
from django.conf import settings
from django.conf.urls import *
from django.core.exceptions import ObjectDoesNotExist
from tastypie.authorization import Authorization
from tastypie import fields
from cbh_core_api.authorization import ProjectAuthorization
from cbh_core_api.resources import SimpleResourceURIField, UserResource, UserHydrate, CBHNoSerializedDictField,  ChemregProjectResource, ChemRegCustomFieldConfigResource, NoCustomFieldsChemregProjectResource
from cbh_utils import elasticsearch_client
import json 
from django.http import HttpResponse, HttpRequest
from tastypie import http
from tastypie.exceptions import Unauthorized, BadRequest
from tastypie.utils.mime import determine_format, build_content_type
from base64 import b64decode, b64encode
import time
from cbh_chem_api.new_serializers import CBHCompoundBatchSerializer
from django_q.tasks import async_iter, result
from django.core.cache import caches
from tastypie.utils import dict_strip_unicode_keys
from rdkit import Chem
from cbh_chembl_model_extension.models import _ctab2image
from django.db import IntegrityError
from django.test import RequestFactory
from django.contrib.auth import login
from django.db.models.loading import get_model
from django.contrib.auth import get_user_model
from copy import deepcopy

EMPTY_ARRAY_B64 = b64encode("[]")


from django_q.tasks import schedule
try:
    
    schedule('cbh_chembl_model_extension.models.index_new_compounds',
            name="index_new_compounds",
             schedule_type='H')
except IntegrityError:
    #Already created
    pass


class CompoundPropertiesResource(ModelResource):
    """
    Resource to provide data about the compound properties 
    as generated by the generateCompoundProperties task in 
    ChEMBL business model
    Used only for a related resource, not declared globally as this would be a security risk
    """
    class Meta:
        queryset = CompoundProperties.objects.all()
        fields = ["alogp", "full_molformula", "full_mwt"]

class MoleculeDictionaryResource(ModelResource):
    """
    Resource to provide data about the Molecule Dictionary
    Provides access to the compoundproperties model for a given CompoundBatch instance
    Used only for a related resource, not declared globally as this would be a security risk
    """
    compoundproperties = fields.ForeignKey(CompoundPropertiesResource, 'compoundproperties',  null=True, readonly=True, full=True)
    authorization = ProjectAuthorization()
    project = fields.ForeignKey(
        ChemregProjectResource, 'project', blank=False, null=False)


    class Meta:
        queryset = MoleculeDictionary.objects.all()
        fields = ["compoundproperties"]




class CBHChemicalSearchResource(Resource):
    """
    Provides a REST interface to POST a molfile for use with chemical search
    """
    id = fields.CharField(help_text="uuid generated on POST of data - comes from the task ID when molecule is searched for")
    query_type = fields.CharField(help_text="Chemical query type - currently supports 'with_substructure' or 'flexmatch'")
    molfile = fields.CharField(help_text="MDL molfile text for the molecule being searched for")
    smiles = fields.CharField(help_text="Generated SMILES string for the molecule being searched for")
    pids = fields.ListField(help_text="Project IDs over which this chemical search should be run")
    filter_is_applied = fields.BooleanField(default=False, help_text="front end helper boolean")
    inprogress = fields.BooleanField(default=False,  help_text="front end helper boolean")

    class Meta:
        """
        authorization is not used in the standard way as we just need to check the submitted project id list
        """
        resource_name = 'cbh_chemical_search'
        authorization = ProjectAuthorization()

    def convert_mol_string(self, strn):
        """
        Take the mdl molfile string and convert it into a SMILES string
        """
        try:
            mol = Chem.MolFromMolBlock(strn)
            smiles = Chem.MolToSmiles(mol)
        except:
            smiles = ""
        
        return smiles

    def get_detail(self, request, **kwargs):
        """
        Used in the saved search method on the front end to return 
        the molecule that should be structure searched
        This caching needs an upgrade to use the database instead as saved search 
        could be retrieved in a different session
        """
        bundle = self.build_bundle(request=request)

        bundle.data = caches[settings.SESSION_CACHE_ALIAS].get("structure_search__%s" % kwargs.get("pk", None))
        bundle = self.alter_detail_data_to_serialize(request, bundle)
        return self.create_response(request, bundle)

    def alter_deserialized_detail_data(self, request, deserialized):
        """
        Standard tastypie hook to alter the input data from a post request after deserialization
        This function adds SMILES patterns and images to the input molfile
        """
        deserialized["smiles"] = self.convert_mol_string(deserialized["molfile"])
        if not deserialized["smiles"]:
            deserialized["error"] = True
            deserialized["image"] = None
        else:
            deserialized["error"] = False
            deserialized["image"] = _ctab2image(deserialized["molfile"],50,False, recalc=None)

        deserialized["inprogress"] = False
        deserialized["filter_is_applied"] = False
        return deserialized

    def post_list(self, request, **kwargs):
        """
        Takes an input molfile from a POST request and processes the structure search then
        saves it to the session cache 
        """
        deserialized = self.deserialize(request, request.body, format=request.META.get('CONTENT_TYPE', 'application/json'))
        deserialized = self.alter_deserialized_detail_data(request, deserialized)
        bundle = self.build_bundle(data=deserialized, request=request)
        updated_bundle = self.obj_create(bundle, **self.remove_api_resource_names(kwargs))
        
        updated_bundle = self.alter_detail_data_to_serialize(request, updated_bundle)
        
        return self.create_response(request, updated_bundle, response_class=http.HttpCreated, location=None)


    def obj_create(self, bundle, **kwargs):
        """
        Generate a task id and send the relevant tasks to django q
        """
        pids = bundle.data.get("pids", None)

        project_ids = []
        if pids:

            project_ids = [int(pid) for pid in pids.split(",")]
        
        allowed_pids = set(self._meta.authorization.project_ids(bundle.request))

        
        for requested_pid in project_ids:
            if requested_pid not in allowed_pids:
                raise Unauthorized("No permissions for requested project") 

        if len(project_ids) == 0:
            project_ids = allowed_pids

        allowed_pids = list(allowed_pids)
        one_eighth = int(len(allowed_pids)/1) + 1
        #Split the projects into 8 equal parts
        pid_chunks = list(chunks(allowed_pids, one_eighth))

        args = [(pid_list, bundle.data["query_type"], bundle.data["smiles"]) for pid_list in pid_chunks]
        bundle.data = self.add_task_id(bundle.data, args)
        return bundle


        
    def add_task_id(self, data, args):
        """
        Calls async_iter to generate a django_q task id and 
        set the qcluster process to work processing the structure search. 
        The user will then be able to search for it but 
        this gives an extra second or so while the user considers pressing the apply key"""
        data["id"] = async_iter('cbh_chem_api.tasks.get_structure_search_for_projects', args)
        caches[settings.SESSION_CACHE_ALIAS].set("structure_search__%s" % data["id"], data)
        return data


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i+n]

class BaseCBHCompoundBatchResource(UserHydrate, ModelResource):
    """New base resource for a compound batch, abstract implementation used
    in the indexing of compound batches as well as in the search API and the saved searches
    ModelResource - these fields are in addition to the fields already present in the CBHCompoundBatch model"""
    uuid = fields.CharField(default="")
    timestamp = fields.CharField(default="")
    project = fields.ForeignKey(
        ChemregProjectResource, 'project', blank=False, null=False)
    creator = SimpleResourceURIField(UserResource, 'created_by_id', null=True, readonly=True)
    projectfull = fields.ForeignKey(
         ChemregProjectResource, 'project', blank=False, null=False, full=True, readonly=True)
    related_molregno = fields.ForeignKey(MoleculeDictionaryResource, 'related_molregno',  null=True, readonly=True, full=True)
    uncurated_fields = CBHNoSerializedDictField('uncurated_fields')
    warnings = CBHNoSerializedDictField('warnings')
    properties = CBHNoSerializedDictField('properties')
    custom_fields = CBHNoSerializedDictField('custom_fields')

    def dehydrate_timestamp(self, bundle):
        """Provide a viewer-friendly timestamp"""
        return str(bundle.obj.created)[0:10]

    def dehydrate_uuid(self, bundle):
        """This is either a blinded batch uuid or a uuid for the compound structure based on the INCHI key"""
        
        if bundle.obj.related_molregno:
            if bundle.obj.related_molregno.chembl:
                if bundle.obj.related_molregno.chembl.chembl_id:
                    return bundle.obj.related_molregno.chembl.chembl_id
        if bundle.obj.blinded_batch_id.strip():
            return bundle.obj.blinded_batch_id

    def create_response(self, request, data, response_class=HttpResponse, **response_kwargs):
        """
        Ensure that data is returned in the right format as an attachment if returning SDF or XLSX data
        Should an object have been created or updated, now is the time to
        reindex it in the elasticsearch index
        """
        desired_format = self.determine_format(request)
        if response_class == http.HttpCreated or response_class == http.HttpAccepted:
            batches = []
            if data.data.get("objects"):
                for b in data.data.get("objects"):
                    batches.append(b.obj)
            else:
                batches.append(data.obj)
            index_batches_in_new_index(batches)

        serialized = self.serialize(request, data, desired_format)
        rc = response_class(content=serialized, content_type=build_content_type(
            desired_format), **response_kwargs)
        if(desired_format == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'):
            rc['Content-Disposition'] = 'attachment; filename=export_from_chembiohub_chemreg%d.xlsx' % int(time.time())
        elif(desired_format == 'chemical/x-mdl-sdfile'):
            rc['Content-Disposition'] = 'attachment; filename=export_from_chembiohub_chemreg%d.sdf' % int(time.time())
        return rc


    def hydrate_blinded_batch_id(self, bundle):
        """Hydrate functions are run before save of a new or updated model instance
        This one ensures that the blinded batch id is filled in in cases where an inventory item 
        with no ModelculeDictionary attribute (related_molregno)""" 
        if bundle.data.get("blinded_batch_id", "") == u"EMPTY_ID":
            uox_id = generate_uox_id()
            bundle.data["blinded_batch_id"] = uox_id
            bundle.obj.blinded_batch_id = uox_id
        return bundle


    def dehydrate_properties(self, bundle):
        """Ensure that the archived property is always presented in a consistent way"""
        archived = bundle.obj.properties.get("archived", "false")
        value = bundle.obj.properties
        value["archived"] = archived
        return value





    def get_project_specific_data(self, request, queries, pids, sorts, textsearch, batch_ids_by_project):
        """When an Excel or SDF download is requested, 
        present the data as a
        project by project summary so that 
        one spreadsheet can be used per project"""
        to_return = []
        pids = list(pids)
        table_schemata = get_schemata(pids, "export", request=request)
        #We have removed the schema from the index for data efficiency so
        #We need to pull it out of the project resource
            
        for index, proj_data in enumerate(table_schemata):
            table_schema_for_project = proj_data["tabular_data_schema"]["for_export"]
            pid = pids[index]
            proj_index = elasticsearch_client.get_project_index_name(pid)
            es_data = elasticsearch_client.get_list_data_elasticsearch(queries,
                    proj_index,
                    sorts=sorts, 
                    textsearch=textsearch,
                    offset=0,
                    limit=10000,
                    batch_ids_by_project=batch_ids_by_project )
            standardised = self.prepare_es_hits(es_data)

            standardised["name"] = proj_data["name"]
            standardised["id"] = pid
            standardised["schema"] = table_schema_for_project
            base = request.META.get("wsgi.url_scheme","http") 
            base += "://"
            base += request.META.get("HTTP_HOST", "")
            for field in standardised["schema"]:
                field["base_url"] = base
            to_return.append(standardised)
        
        if request.GET.get("format", "") != "sdf":
            #For the Excel sheet print out the query used
            query_reps = [{"fieldn": "Project", "pick_from_list": ",".join([str(ret["name"]) for ret in to_return]) }]
            for i, q in enumerate(queries):
                qrep = {qtype["value"] : u"" for qtype in settings.CBH_QUERY_TYPES}
                qrep["fieldn"] = ""
                for key, value in q.items():
                    if key == "pick_from_list":
                        qrep[key] = ", ".join(value)
                    elif key == "field_path":
                        if value:
                            knownBy = settings.TABULAR_DATA_SETTINGS["schema"].get(value,{}).get("knownBy", "")
                            if not knownBy:
                                knownBy = str(value.split(".")[-1])
                            qrep["fieldn"] = "".join([l for l in knownBy])
                    else:
                        qrep[key] = str(value)

                qrep["id"] = i+1
                query_reps.append(qrep)

            schem = [{"data": "id", "knownBy" : "Row"}, {"data": "fieldn", "knownBy" : "Col"}] + [{"data": qtype["value"], "knownBy" : qtype["name"]} for qtype in settings.CBH_QUERY_TYPES]
            query_summary = {
                    "name" : "_Query Used",
                    "objects" : query_reps,
                    "schema" : schem
                    }
            to_return.append(query_summary)
        return to_return



    def get_detail(self, request, **kwargs):
        """
        Returns a single serialized resource.
        Calls ``cached_obj_get/obj_get`` to provide the data, then handles that result
        set and serializes it.
        Should return a HttpResponse (200 OK).
        Get a single CBHCompoundbatches from elasticsearch by running a query

        """
        basic_bundle = self.build_bundle(request=request)

        kwargs = self.remove_api_resource_names(kwargs)
        allowed_pids = self._meta.authorization.project_ids(request)
        concatenated_indices = elasticsearch_client.get_list_of_indicies(allowed_pids)
        data = elasticsearch_client.get_detail_data_elasticsearch(concatenated_indices, kwargs["pk"])   
        

        requested_pid = data["projectfull"]["id"]

        if requested_pid not in allowed_pids:
            raise Unauthorized("No permissions for requested project") 

        bundle = self.alter_detail_data_to_serialize(request, data)
        return self.create_response(request, bundle)


    

    def get_list(self, request, **kwargs):
        """
        Returns a serialized list of resources.
        Calls ``obj_get_list`` to provide the data, then handles that result
        set and serializes it.
        Should return a HttpResponse (200 OK).
        Get a list of CBHCompoundbatches from elasticsearch by running a query
        """

        base_bundle = self.build_bundle(request=request)
        pids = request.GET.get("pids", "")
        project_ids = []
        if pids:

            project_ids = [int(pid) for pid in pids.split(",")]
        
        allowed_pids = set(self._meta.authorization.project_ids(request))
        
        for requested_pid in project_ids:
            if requested_pid not in allowed_pids:
                raise Unauthorized("No permissions for requested project") 



        if len(project_ids) == 0:
            project_ids = allowed_pids

        queries = json.loads(b64decode(request.GET.get("encoded_query", EMPTY_ARRAY_B64)))

        extra_queries = kwargs.get("extra_queries", False)
        if extra_queries:
            #extra queries can be added via kwargs
            queries += extra_queries

        #Search for whether this item is archived or not ("archived is indexed as a string")
        archived = request.GET.get("archived", "false")
        queries.append({"query_type": "phrase", "field_path": "properties.archived", "phrase": archived})

       

        sorts = json.loads(b64decode(request.GET.get("encoded_sorts", EMPTY_ARRAY_B64)))
        if len(sorts) == 0:
            sorts = [{"field_path":"id","sort_direction":"desc"}]
        textsearch = b64decode(request.GET.get("textsearch", ""))
        limit = request.GET.get("limit", 10)
        offset = request.GET.get("offset", 0)
        autocomplete = request.GET.get("autocomplete", "")
        autocomplete_field_path = request.GET.get("autocomplete_field_path", "")
        autocomplete_size = request.GET.get("autocomplete_size", settings.MAX_AUTOCOMPLETE_SIZE)

        pr = ChemregProjectResource()
        resp = pr.get_list(request, do_cache=True)
        project_content = json.loads(resp.content)
        restricted_fieldnames = project_content["user_restricted_fieldnames"]

        #The project ids list needs to be reduced down
        #Because we dont support OR queries then every time you query a project for 
        #a field then only the projects that have that field (and unrestricted) need to be shown
        for q in queries:
             project_ids = self._meta.authorization.check_if_field_restricted(q["field_path"], project_ids, restricted_fieldnames)
        if autocomplete_field_path:
            project_ids = self._meta.authorization.check_if_field_restricted(autocomplete_field_path, project_ids, restricted_fieldnames)

        concatenated_indices = elasticsearch_client.get_list_of_indicies(project_ids)

        chemical_search_id = request.GET.get("chemical_search_id", False)
        batch_ids_by_project = None
        if chemical_search_id:
            batch_ids_by_project = result(chemical_search_id, wait=20000)
            if not batch_ids_by_project:
                return HttpResponse('{"error": "Unable to process tructure search"}', status=503)
        if request.GET.get("format", None) != "sdf" and request.GET.get("format", None) != "xlsx":
            data = elasticsearch_client.get_list_data_elasticsearch(queries,
                concatenated_indices,
                sorts=sorts, 
                offset=offset, 
                limit=limit, 
                textsearch=textsearch, 
                autocomplete=autocomplete,
                autocomplete_field_path=autocomplete_field_path,
                autocomplete_size=autocomplete_size,
                batch_ids_by_project=batch_ids_by_project
                 )
            
            
            if autocomplete_field_path:
            
                bucks = data["aggregations"]["filtered_field_path"]["field_path_terms"]["buckets"]

                bundledata = {"items" : bucks,
                            "autocomplete" : autocomplete,
                            "unique_count" : data["aggregations"]["filtered_field_path"]["unique_count"]["value"]}
        
            else:

                bundledata = self.prepare_es_hits(data)
                bundledata["objects"] = self._meta.authorization.removed_restricted_fields_if_present(bundledata["objects"], restricted_fieldnames)
        else:
            #Limit , offset and autocomplete have no effect for a project export
            data = self.get_project_specific_data(request,  queries, project_ids, sorts, textsearch, batch_ids_by_project)

            return self.create_response(request, data) 


        return self.create_response(request, bundledata) 

    def prepare_es_hits(self, hits):
        return {"objects": 
                            [hit["_source"]["dataset"] for hit in hits["hits"]["hits"]],
                            "meta" : {"total_count" : hits["hits"]["total"]}
                            }

    class Meta:
        authorization = ProjectAuthorization()
        queryset = CBHCompoundBatch.objects.all()
        
        include_resource_uri = True
        serializer = CBHCompoundBatchSerializer()
        always_return_data = True







class CBHCompoundBatchResource(BaseCBHCompoundBatchResource):
    """Implementation of the compound batch API used for inventory and compound batch objects
    fiter out saved searches from this list"""
    class Meta(BaseCBHCompoundBatchResource.Meta):
        resource_name = 'cbh_compound_batches_v2'


    
    def get_list(self, request, **kwargs):
        """Request data where the project is not of the saved search type"""
        return super(CBHCompoundBatchResource, self).get_list(request, extra_queries=[{"query_type": "pick_from_list", "field_path" : "projectfull.project_type.saved_search_project_type", "pick_from_list" : ["false"] }])



class CBHSavedSearchResource(BaseCBHCompoundBatchResource):
    """Implementation of the compound batch API used for saved search objects,
    filter out non saved searches form this list"""
    class Meta(BaseCBHCompoundBatchResource.Meta):
        resource_name = 'cbh_saved_search'


    def get_list(self, request, **kwargs):
        """Request data where the project is of the saved search type"""

        return super(CBHSavedSearchResource, self).get_list(request, extra_queries=[{"query_type": "pick_from_list", "field_path" : "projectfull.project_type.saved_search_project_type", "pick_from_list" : ["true"] }])




class IndexingCBHCompoundBatchResource(BaseCBHCompoundBatchResource):
    """Implementation of the BaseCBHCompoundBatchResource used to generate JSON objects to be
    indexed in elasticsearch"""
    project = SimpleResourceURIField(ChemregProjectResource, "project_id")
    projectfull = SimpleResourceURIField(ChemregProjectResource, "project_id")

    def prepare_fields_for_index(self, batch_dicts):
        """Fields are stored as strings in hstore so convert the list or object fields back from JSON"""
        for bd in batch_dicts:
            if not bd.get("related_molregno", None):
                bd["related_molregno"] = {"compoundproperties" : {}}
            for field in bd["projectfull"]["custom_field_config"]["project_data_fields"]:
                
                if field["edit_schema"]["properties"][field["name"]]["type"] == "object":
                    if bd["custom_fields"].get(field["name"], False):
                        try:
                            bd["custom_fields"][field["name"]] = json.loads(bd["custom_fields"][field["name"]])
                            continue
                        except ValueError:
                            pass
                    bd["custom_fields"][field["name"]] = field["edit_schema"]["properties"][field["name"]]["default"]
                elif field["edit_schema"]["properties"][field["name"]]["type"] == "array":
                    if bd["custom_fields"].get(field["name"], False):
                        try:
                            bd["custom_fields"][field["name"]] = json.loads(bd["custom_fields"][field["name"]])
                            continue
                        except ValueError:
                            pass
                    bd["custom_fields"][field["name"]] = []
                elif not bd["custom_fields"].get(field["name"], False):
                    #Clean up existing fields to a blank string
                    bd["custom_fields"][field["name"]] = field["edit_schema"]["properties"][field["name"]].get("default", "")

        return batch_dicts


    def index_batch_list(self, request, batch_list, project_and_indexing_schemata, refresh=True):
        """Index a list or queryset of compound batches into the elasticsearch indices (one index per project)"""
        bundles = [
            self.full_dehydrate(self.build_bundle(obj=obj, request=request), for_list=True)
            for obj in batch_list
        ]

        #retrieve schemas which tell the elasticsearch request which fields to index for each object (we avoid deserializing a single custom field config more than once)
        #Now make the schema list parallel to the batches list
        batch_dicts = [self.Meta.serializer.to_simple(bun, {}) for bun in bundles]
        batch_dicts, indexing_schemata = add_cached_projects_to_batch_list(batch_dicts, project_and_indexing_schemata)
        batch_dicts = self.prepare_fields_for_index(batch_dicts)

        project_data_field_sets = [batch_dict["projectfull"]["custom_field_config"].pop("project_data_fields", True) for batch_dict in batch_dicts]
        tab_data = [batch_dict["projectfull"].pop("tabular_data_schema", True) for batch_dict in batch_dicts]

        index_names = []

        for batch in batch_dicts:
            batch["projectfull"]["custom_field_config"] = batch["projectfull"]["custom_field_config"]["resource_uri"]
            index_name = elasticsearch_client.get_project_index_name(batch["projectfull"]["id"])
            index_names.append(index_name)
        
        batch_dicts = elasticsearch_client.index_dataset(batch_dicts, indexing_schemata, index_names, refresh=refresh)
            

    def reindex_elasticsearch(self, request, **kwargs):
        """Reindex all of the data into elasticsearch"""
        desired_format = self.determine_format(request)
        batches = self.get_object_list(request)
        # we only want to store certain fields in the search index
        from django.core.paginator import Paginator
        paginator = Paginator(batches, 5000) # chunks of 1000
        #Get all schemata for all projects 
        indexing_schemata = get_indexing_schemata(None)
        for page in range(1, paginator.num_pages +1):
        
            result_list = index_batches_in_new_index(paginator.page(page).object_list, project_and_indexing_schemata=indexing_schemata)
            print "%d of %s" % (page, paginator.num_pages)
        return HttpResponse(content="test", )
        
    class Meta(BaseCBHCompoundBatchResource.Meta):
        pass

def add_cached_projects_to_batch_list(batch_dicts, project_and_indexing_schemata):
    schemata_for_indexing = []
    for batch_dict in batch_dicts:
        projdata = project_and_indexing_schemata[batch_dict["project"]]
       
        schemata_for_indexing.append(projdata["tabular_data_schema"]["for_indexing"])
        batch_dict["projectfull"] = deepcopy(projdata)
    return (batch_dicts, schemata_for_indexing)


def get_schemata(project_ids, fieldlist_name="indexing", request=None):
    if project_ids is None:
        project_ids = get_model("cbh_core_model","Project").objects.filter().values_list("id", flat=True)
    crp = ChemregProjectResource()
    request_factory = RequestFactory()
    user = get_user_model().objects.filter(is_superuser=True)[0]
    
 
    for pid in project_ids:
        if request:
            req = request
        else:
            req = request_factory.get("/")
            req.user = user
        req.GET = req.GET.copy()
        req.GET["tabular_data_schema"] = True

        data = crp.get_detail(req, pk=pid)

        projdata = json.loads(data.content)
        projdata["tabular_data_schema"]["for_%s" % fieldlist_name] = [ projdata["tabular_data_schema"]["schema"][field] 
                                  for field in projdata["tabular_data_schema"]["included_in_tables"][fieldlist_name]["default"]]
        yield projdata


def get_indexing_schemata(project_ids, fieldlist_name="indexing"):
    """Get a cached version of the project schemata in the right format 
    for elasticsearch indexing or for retrieving the Excel backup of the data"""

    proj_datasets = get_schemata(project_ids, fieldlist_name="indexing")
        #Do the dict lookups now to avoid multiple times later
    projdatadict = {}
    for projdata in proj_datasets:
        projdatadict[projdata["resource_uri"]] = projdata

    return projdatadict

        


def index_batches_in_new_index(batches, project_and_indexing_schemata=None):
    """function to index all the data, creating a HTTPRequest programatically"""
    request = HttpRequest()
    if project_and_indexing_schemata is None:
        project_and_indexing_schemata = get_indexing_schemata({ batch.project_id  for batch in batches })

    IndexingCBHCompoundBatchResource().index_batch_list(request, batches, project_and_indexing_schemata)




##Multiple batch creation


class CBHMultipleBatchUploadResource(ModelResource):
    pass

