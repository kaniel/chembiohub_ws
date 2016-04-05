from django.conf import settings
import elasticsearch
import json
import time
try:
    ES_PREFIX = settings.ES_PREFIX
except AttributeError:
    ES_PREFIX = "dev"
ES_MAIN_INDEX_NAME = "crv2"
from jsonpointer import resolve_pointer

AGG_TERMS_SEPARATOR = "|||"

BATCH_TYPE_NAME = "newbatches"
def get_main_index_name():
    return "%s__%s" % (ES_PREFIX, ES_MAIN_INDEX_NAME)

def get_project_index_name(project_id):
    return "%s__%s__project__%d" % (ES_PREFIX, ES_MAIN_INDEX_NAME, project_id)

def get_list_of_indicies(project_ids):
    list_of_indices = [get_project_index_name(project_id) for project_id in project_ids]
    return ",".join(list_of_indices)



def fix_data_types_for_index( value):
    """Elasticsearch will not index dictionaries"""
    if value is None:
        return None

    if not unicode(value):
        #pick up empty strings etc but not false

        return None
    if unicode(value) == "True" or unicode(value) == "False":
        return unicode(value).lower()
    if isinstance(value, basestring):
        if value.strip():
            return value
        else:
            return None
    if type(value) is dict:
        return json.dumps(value)
    if type(value) is list:
        return [fix_data_types_for_index(v) for v in value]
    return unicode(value)




def build_indexed_fields(document, schema):
    document["indexed_fields"] = []
    for field in schema:
        slashed_json_pointer = "/%s" % field["data"].replace(".", "/")

        raw_value = resolve_pointer(document,slashed_json_pointer, default=None)
        
        value = fix_data_types_for_index(raw_value)
        if value:
            #We do not add an index for any blank, empty or non existant field, that way
            #we can be sure that the blanks filter will pick up all of the true blank fields
            document["indexed_fields"].append({"name" : field["knownBy"], "value": value, "field_path": field["data"] })

def build_all_indexed_fields(batch_dicts, schema_list):
    assert(len(batch_dicts) == len(schema_list))
    for index,  schema in enumerate(schema_list):
        build_indexed_fields(batch_dicts[index], schema)




def index_dataset( batch_dicts, schema_list, index_names):
    build_all_indexed_fields(batch_dicts, schema_list)
    es_reindex = create_index(
                batch_dicts, index_names)
    if es_reindex.get("errors"):
        print "ERRORS"
        print json.dumps(es_reindex)
        raise Exception("indexing failed")



def create_index(batches, index_names):
    es = elasticsearch.Elasticsearch()
    t = time.time()

    for index_name in index_names:
        es.indices.create(
        index_name,
        body=settings.ELASTICSEARCH_INDEX_MAPPING,
        ignore=400)

    bulk_items = []
    if len(batches) > 0:
        for counter, item in enumerate(batches):
            batch_doc = {
                "update":
                {
                    "_index": index_names[counter],
                    "_type": BATCH_TYPE_NAME
                }
            }
            if item.get("id", None):
                batch_doc["update"]["_id"] = str(item["id"])
            bulk_items.append(batch_doc)
            bulk_items.append({"doc" : item, "doc_as_upsert" : True })
        # Data is not refreshed!
        data = es.bulk(body=bulk_items, refresh=True)

    return {}



def get_template_nested_must_clause(field_path, field_query):
    """Match both the original field path and whatever query we are trying to run on that field"""
    column_query = {
                                "term" : {
                                    "indexed_fields.field_path" : field_path
                                }
                            }

    template_must_clause = {
        "nested" : {
                "path" : "indexed_fields",
                "query" : {
                    "bool" : {
                        "must" : [field_query, column_query]
                    }
                }
            }
        }
    return template_must_clause


def build_phase_prefix_query(phrase):
    return {
                    "multi_match" :
                    { 
                        "type": "phrase_prefix", 
                        "fields": ["indexed_fields.value",] , 
                        "query" : phrase
                    }
                }


def build_es_request(queries, textsearch="", batch_ids_by_project=None):
    must_clauses = []
    if batch_ids_by_project:
        #The postgres backend has converted the chemical search
        #Into a list of ids by project
        #We then join these ids queries on a per project basis
        match_these_ids_by_index = []
        for search_dict in batch_ids_by_project:
            index_name = get_project_index_name(search_dict["project_id"])
            batch_ids = search_dict["batch_ids"]
            index_query = { "indices":
                                {
                                    "indices": [index_name],
                                    "query": {
                                        "ids" : {
                                            "type" : BATCH_TYPE_NAME,
                                            "values" : [str(id) for id in batch_ids]
                                        }
                                    },
                                    "no_match_query" : "none"
                                }
                            }
            match_these_ids_by_index.append(index_query)

        #Each document should be in the specified ID list for the project it is in
        by_index_batch_id_query = {   "bool" :{
                "should" : match_these_ids_by_index
            }
        }
        must_clauses.append(by_index_batch_id_query)



    if textsearch:
        subquery = {
                    "nested" : {
                            "path" : "indexed_fields",
                            "query" : {
                                
                                    "multi_match" : { 
                                        "type": "phrase_prefix", 
                                        "fields": ["indexed_fields.value",] , 
                                        "query" : textsearch 
                                    }
                                }
                            }
                        }
                    
        must_clauses.append(subquery)


    for query in queries:
        new_query = None
        if query["query_type"] == 'phrase':
            new_query = build_phase_prefix_query(query["phrase"])
        
        elif query["query_type"] == 'pick_from_list': 
            new_query = {
                    "terms" :
                    { 
                        "indexed_fields.value.raw": query["pick_from_list"] 
                    }
                }

            
        elif query["query_type"] ==  'between':
            new_query = {
                    "range" :
                    { 
                        "indexed_fields.value.raw" : 
                            {
                                "gt" : query["greater_than"],
                                "lt"  : query["less_than"]
                            }
                    }
                }
        elif query["query_type"] ==  'greater_than':
            new_query = {
                    "range" :
                    { 
                        "indexed_fields.value.raw" : 
                            {
                                "gt" : query["greater_than"],
                            }
                    }
                }
        elif query["query_type"] ==  'less_than':
            new_query = {
                    "range" :
                    { 
                        "indexed_fields.value.raw" : 
                            {
                                "lt"  : query["less_than"]
                            }
                    }
                }



        if new_query:
            q = get_template_nested_must_clause(query["field_path"], new_query)
            must_clauses.append(q)
        else:
            new_query =  {
                    "nested" : {
                            "path" : "indexed_fields",
                            "query" : {
                                "term" : {
                                    "indexed_fields.field_path" : query["field_path"]
                                }
                            }
                        }
                    } 
            if query["query_type"] ==  'blanks':
                must_clauses.append({
                    "bool" : {
                            "must_not" : [new_query]
                        }
                    })
            elif query["query_type"] ==  'nonblanks':
                must_clauses.append(new_query)
        

    
            
    return must_clauses
#Take the first 256 characters of the field and zero pad it if it is a string or float
#We use the first 256 characters as this is what will be matched
#In the pick from list query given that this is the max length of the raw field in elasticsearch
ZERO_PAD_GROOVY_SCRIPT = """
    tmp = ''; 
    for(item in _source.indexed_fields){
        if(item.field_path==field_path){
            if(item.value instanceof String){ 
                //The groovy take methoid is a failsafe substring
                //We do this to ensure the string is same length as indexed data
                tmp = item.value.take(""" + str(settings.ELASTICSEARCH_MAX_FIELD_LENGTH) + """)
                //Zero pad integaers or floats
                if(tmp.isInteger()){
                    tmp = String.format('%014d',tmp.toInteger());
                }
                else if(tmp.isFloat()) {
                    def (value1, value2) = tmp.tokenize('.'); 
                    tmp = String.format('%014d',value1.toInteger()) + '.' + value2 
                }
                if(separate){
                    //Add a separator so that we can retrieve the data in uppercase format 
                    tmp = tmp.toLowerCase() + '""" + AGG_TERMS_SEPARATOR + """' + tmp;
                }else if(lowerCase){
                    tmp = tmp.toLowerCase();
                }
                
            } else if(item.value instanceof List){
                //If this is a list then run the string operations for each element of the list
                if(item.value.size() > 0){
                    def stockArr = [];
                    tmp = stockArr;
                    for (v in item.value){
                        if(separate){
                            stockArr.push( v.toString().toLowerCase() + '""" + AGG_TERMS_SEPARATOR + """' + v);
                        }else if(lowerCase){
                            stockArr.push( v.toString().toLowerCase() );
                        }
                    }
                }
            }
        }
    }; 
    
    return tmp;"""


def build_sorts(sorts):
    """This script (written in groovy) picks out 
    the field value in elasticsearch and spits it 
    out as a zero padded string if it is either an integer or a float"""
    elasticsearch_sorts = [
        {
            "_script":{"script": ZERO_PAD_GROOVY_SCRIPT ,
            "params" : {"field_path" : sort["field_path"],
                                    "lowerCase" : True,
                                    "separate" : False },
            "type" : "string", "order" : sort["sort_direction"]}
        }
        for sort in sorts
    ]
    return elasticsearch_sorts

def get_nested_aggregation_for_field_path(autocomplete_field_path, autocomplete="", autocomplete_size=settings.MAX_AUTOCOMPLETE_SIZE):
    """Based upon an input term and field_path, generate an aggregation to group by that field returning zero padded numbers to get the order right"""
    base_agg = {
                "field_path_terms" : {
                    "terms" : {
                        "script" : ZERO_PAD_GROOVY_SCRIPT ,
                        "params" : { "field_path" : autocomplete_field_path,
                                    "lowerCase" : True,
                                    "separate" : True },
                        "size" : autocomplete_size,
                         "order" : { "_term" : "asc" }
                    }
                },
                "unique_count": {"cardinality" : {
                    "script" : ZERO_PAD_GROOVY_SCRIPT,
                    "params" : { "field_path" : autocomplete_field_path ,
                                    "lowerCase" : False,
                                    "separate" : False},
                }}
            }

    if autocomplete:
        #If there is a search term, then, having applied a set of filters to the data (project, other search terms)
        #We then try to apply a term filter to the data being aggregated for the search term being looked for
        query = get_template_nested_must_clause(autocomplete_field_path, build_phase_prefix_query(autocomplete))
    else:
        query = { "match_all": {} }

    base_agg = { 
                "filtered_field_path":
                        {
                        "filter" : query,
                        "aggs" : base_agg

                        }
                }
                    
  
    return base_agg


def remove_existing_queries_for_agg(queries, autocomplete_field_path):
    """We are trying to provide an aggregated autocomplete for a particular field
    Therefore if a query has already been applied to that field, we need to remove that subquery"""
    if autocomplete_field_path:
        new_queries = []
        for q in queries:
            if q["field_path"] != autocomplete_field_path:
                new_queries.append(q)
        return new_queries
    return queries


def get_list_data_elasticsearch(queries, index, sorts=[], autocomplete="", autocomplete_field_path="", autocomplete_size=settings.MAX_AUTOCOMPLETE_SIZE, textsearch="", offset=0, limit=10, batch_ids_by_project=None):
    es = elasticsearch.Elasticsearch()
    queries = remove_existing_queries_for_agg(queries, autocomplete_field_path)
    if len(queries) > 0  or len(textsearch) > 0:
        es_request = {  
                    "query":{
                        
                        "bool" : {
                            "filter" : [    
                                   build_es_request(queries, textsearch=textsearch, batch_ids_by_project=batch_ids_by_project)
                            ]
                        }
                    },
                    "sort" : build_sorts(sorts),
                    "_source" : {
                        "include": [ "*" ],
                        "exclude": [ "indexed_fields.*" , "bigimage" ]
                    },
                }
        
    else:
        es_request = {
                        "query" : {"match_all": {}},
                        "sort" : build_sorts(sorts),
                        "_source" : {
                            "include": [ "*" ],
                            "exclude": [ "indexed_fields.*" , "bigimage" ]
                        }
                    }
    if autocomplete_field_path:
        es_request["aggs"] = get_nested_aggregation_for_field_path(autocomplete_field_path, autocomplete=autocomplete, autocomplete_size=autocomplete_size)
    es_request["from"] = offset
    es_request["size"] = limit

    data = es.search(index, body=es_request, ignore_unavailable=True)

    if autocomplete_field_path:
        newbucks = []
        for bucket in data["aggregations"]["filtered_field_path"]["field_path_terms"]["buckets"]:
            #Un zero pad the returned items
            bucket["key"] = remove_terms_separator(bucket["key"])
            bucket["key"] = unzeropad(bucket["key"])
            if bucket["key"]:
                #Dont accept empty strings
                newbucks.append(bucket)
        data["aggregations"]["filtered_field_path"]["field_path_terms"]["buckets"] = newbucks

    return data

def remove_terms_separator(input_string):
    return input_string.split(AGG_TERMS_SEPARATOR)[1]

def unzeropad(input_string):
    replace_up_to = 0
    if input_string.replace(".", "", 1).isdigit():
        for index, char in enumerate(input_string):
            if char != "0":
                replace_up_to = index
                #found the first non zero so break
                break 
    return input_string[replace_up_to:]

def get_detail_data_elasticsearch(index, id):
    es = elasticsearch.Elasticsearch()
    es_request = {
                        "query" : {"term": {"_id" : id}},
                        "_source" : {
                            "include": [ "*" ],
                            "exclude": [ "indexed_fields.*" ]
                        }
                    }
    data = es.search(index, body=es_request, ignore_unavailable=True)

    return data["hits"]["hits"][0]["_source"]
