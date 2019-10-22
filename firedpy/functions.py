# -*- coding: utf-8 -*-
from bs4 import BeautifulSoup
from collections import OrderedDict
import datetime as dt
import ftplib
import gc
import geopandas as gpd
from glob import glob
from http.cookiejar import CookieJar
from multiprocessing import cpu_count, Pool
from netCDF4 import Dataset
import numpy as np
import os
import pandas as pd
import rasterio
from rasterio.merge import merge
from shapely.geometry import Point, Polygon, MultiPolygon
import sys
from tqdm import tqdm
import requests
import urllib.request as urllib2
import warnings

# The python gdal issue (matching system gdal version)
try:
    from osgeo import gdal, ogr, osr
except ImportError:
    raise ImportError(""" Unfortunately, you still need to install GDAL for
                      Python. Try pip install `pygdal==version` where the
                      version matches the first three digits of the output from
                      the command `gdalinfo --version`. To see available pygdal
                      versions run `pip install pygdal== '
                      """)

warnings.filterwarnings("ignore", category=FutureWarning)
import xarray as xr
pd.options.mode.chained_assignment = None


# Functions
class ModelBuilder:
    def __init__(self, dest, proj_dir, tiles, spatial_param=5,
                 temporal_param=11, landcover=None, ecoregion=None):
        self.dest = dest
        self.proj_dir = proj_dir
        self.tiles = tiles
        self.spatial_param = spatial_param
        self.temporal_param = temporal_param
        self.landcover = landcover
        self.ecoregion = ecoregion
        self.getFiles()
        self.setGeometry()

    def getFiles(self):
        # Get the requested files names
        files = []
        for t in self.tiles:
            path = os.path.join(
                    self.proj_dir, "rasters/burn_area/netcdfs/" + t + ".nc")
            files.append(path)
        files.sort()
        self.files = files

    def setGeometry(self):
        # Use the first file to extract some more parameters for later
        builder = EventGrid(nc_path=self.files[0],
                            proj_dir=self.proj_dir,
                            spatial_param=self.spatial_param,
                            temporal_param=self.temporal_param)
        self.crs = builder.data_set.crs
        self.geom = self.crs.geo_transform
        self.res = self.geom[1]
        self.sp_buf = builder.spatial_param * self.res

    def buildEvents(self):
        """
        Use the EventGrid class to classify events tile by tile and then merge
        them all together for a seamless set of wildfire events.
        """
        # Make sure the desstination folder exists
        if not(os.path.exists(os.path.dirname(self.dest))):
            os.makedirs(os.path.dirname(self.dest))

        # Create an empty list and data frame for the 
        tile_list = []
        columns = ["id", "date", "x", "y", "edge", "tile"]
        df = pd.DataFrame(columns=columns)
        base = dt.datetime(1970, 1, 1)
    
        # Loop through each netcdf file and build individual tile events
        for file in self.files:
            tile_id = file[-9:-3]
            if os.path.exists(
                    os.path.join(
                        self.proj_dir, "tables/events/" + tile_id + ".csv")
                    ):
                print(tile_id  + " event table exists, skipping...")
            elif not os.path.exists(
                    os.path.join(
                        self.proj_dir, "rasters/burn_area/netcdfs/" + tile_id +
                                       ".nc")
                    ):
                pass
            else:
                print("\n" + tile_id)
    
                # Create a new event object
                builder = EventGrid(nc_path=file,
                                    proj_dir=self.proj_dir,
                                    spatial_param=self.spatial_param,
                                    temporal_param=self.temporal_param)
    
                # Classify event perimeters
                perims = builder.get_event_perimeters()
    
                # Remove empty perimeters
                perims = [p for p in perims if type(p.coords[0]) is not str]
                tile_list.append(perims)
    
                # Extract just the event ID, days, and x,y MODIS coordinates
                plist = [(p.get_event_id(), p.coords) for p in perims]
    
                # Identify edge cases, so either x or y is within 5 cells
                maxys = builder.data_set["y"].data[:builder.spatial_param]
                minys = builder.data_set["y"].data[-builder.spatial_param:]
                maxxs = builder.data_set["x"].data[-builder.spatial_param:]
                minxs = builder.data_set["x"].data[:builder.spatial_param]
                yedges = list(maxys) + list(minys)
                xedges = list(maxxs) + list(minxs)
    
                # Create an empty data frame
                print("Building data frame...")
                events = []
                coords = []
                edges = []
                ys = []
                xs = []
                dates = []
                for p in plist:
                    coord = [list(c) for c in p[1]]
                    edge = [edgeCheck(yedges, xedges, c, self.sp_buf) for
                            c in coord]
                    if any(edge):
                        edge = [True for e in edge]
                    event = list(np.repeat(p[0], len(coord)))
                    y = [c[0] for c in coord]
                    x = [c[1] for c in coord]
                    date = [base + dt.timedelta(c[2]) for c in coord]
                    events.append(event)
                    coords.append(coord)
                    edges.append(edge)
                    ys.append(y)
                    xs.append(x)
                    dates.append(date)
    
                # Flatten each list of lists
                events = flttn(events)
                coords = flttn(coords)
                edges = flttn(edges)
                ys = flttn(ys)
                xs = flttn(xs)
                dates = flttn(dates)
                edf = pd.DataFrame(
                        OrderedDict({"id": events, "date": dates, "x": xs,
                                     "y": ys, "edge": edges, "tile": tile_id})
                        )
                if not os.path.exists(
                        os.path.join(self.proj_dir, "tables/events")
                        ):
                    os.mkdir(os.path.join(self.proj_dir, "tables/events")
                    )
                edf.to_csv(
                    os.path.join(
                        self.proj_dir, "tables/events/" + tile_id + ".csv"),
                    index=False)
    
        # Clear memory
        gc.collect()
    
        # Now read in the event data frames (use dask, instead, save memory)
        print("Reading saved event tables back into memory...")
        efiles = glob(os.path.join(self.proj_dir, "tables/events/*csv"))
        efiles = [e for e in efiles if e[-10:-4] in  self.tiles]
        edfs = [pd.read_csv(e) for e in efiles]
    
        # Merge with existing records
        print("Concatenating event tables...")
        df = pd.concat(edfs)
        def toDays(date, base):
            date = dt.datetime.strptime(date, "%Y-%m-%d")
            delta = (date - base)
            days = delta.days
            return days
    
        print("Creating unique ids...")
        df["id"] = df["tile"] + "_" + df["id"].astype(str)
    
        print("Converting days since 1970 to dates...")
        df["days"] = df["date"].apply(toDays, base=base)
    
        # Cut the edge events out into a separate df
        print("Separating tile edge events from center events...")
        edges = df[df["edge"] == True]
        not_edges = df[df["edge"] == False]
    
        # Merge where needed
        print("Merging edge-case tile events...")
        eids = list(edges["id"].unique())
        for iden in tqdm(eids, position=0):
            # Split, one vs all
            edf = edges[edges["id"] == iden]
            edf2 = edges[edges["id"] != iden]
            days = edf["days"]
    
            # Sometimes these are empty
            try:
                d1 = min(days)
                d2 = max(days)
            except:
                pass
    
            # If events aren't close enough in time the list will be empty
            edf2 = edf2[(abs(edf2["days"] - d1) < self.temporal_param) |
                        (abs(edf2["days"] - d2) < self.temporal_param)]
            eids2 = list(edf2["id"].unique())
    
            # If there are event close in time, are they close in space?
            for iden2 in eids2:
                edf2 = edges[edges["id"] == iden2]
                ydiffs = [y - edf2["y"].values for y in edf["y"].values]
                xdiffs = [x - edf2["x"].values for x in edf["x"].values]
                ychecks = [spCheck(yds, self.sp_buf) for yds in ydiffs]
                xchecks = [spCheck(xds, self.sp_buf) for xds in xdiffs]
                checks = [ychecks[i] * xchecks[i] for i in range(len(ychecks))]
                if any(checks):
                    # Merge events! Merge into the earliest event
                    d12 = edf2["days"].min()
                    if d1 < d12:
                        edges["id"][edges["id"] == iden2] = iden
                    else:
                        edges["id"][edges["id"] == iden] = iden2
    
        # Concatenate edge df back into main df
        print("Recombining edge and center cases...")
        df = pd.concat([not_edges, edges])
    
        # Reset id values in chronological order
        print("Resetting ids in chronological order..")
        df["first"] = df.groupby("id").days.transform("min")
        firsts = df[["id", "first"]].drop_duplicates()
        firsts = firsts.sort_values("first")
        firsts["new_id"] = range(1, firsts.shape[0] + 1)
        idmap = dict(zip(firsts["id"], firsts["new_id"]))
        df["id"] = df["id"].map(idmap)
        df = df.sort_values("id")
    
        # put these in order
        df = df[["id", "tile", "date", "x", "y"]]
    
        # Finally save
        print("Saving merged event table to " + self.dest)
        df.to_csv(self.dest, index=False)
    
    
    def buildAttributes(self, lc_dir, eco_dir):
        '''
        Take the data table, add in attributes, and overwrite file.
        '''
        # There has to be an event table first
        if os.path.exists(self.dest):
            df = pd.read_csv(self.dest)
        else:
            print("Run EventBuilder first.")
            return

        # Space is tight and we need the spatial resolution
        res = self.res

        # Group by date first for pixel count
        df['pixels'] = df.groupby(['id', 'date'])['id'].transform('count')

        # Then group by id for event-level attributes
        group = df.groupby('id')
        max_rate_dates = group[['date', 'pixels']].apply(maxGrowthDate)
        df['total_pixels'] = group['pixels'].transform('sum')
        df['date'] = df['date'].apply(
                lambda x: dt.datetime.strptime(x, '%Y-%m-%d')
                )
        df['ignition_date'] = group['date'].transform('min')
        df['ignition_day'] = df['ignition_date'].apply(
                lambda x: dt.datetime.strftime(x, '%j')
                )
        df['ignition_month'] = df['ignition_date'].apply(lambda x: x.month)
        df['ignition_year'] = df['ignition_date'].apply(lambda x: x.year)
        df['last_date'] = group['date'].transform('max')
        df['duration'] = df['last_date'] - df['ignition_date']
        df['duration'] = df['duration'].apply(lambda x: x.days + 1)
        df['total_area_km2'] = df['total_pixels'].apply(toKms, res=res)
        df['total_area_acres'] = df['total_pixels'].apply(toAcres, res=res)
        df['total_area_ha'] = df['total_pixels'].apply(toHa, res=res)
        df['fsr_pixels_per_day'] = df['total_pixels'] / df['duration']
        df['fsr_km2_per_day'] = df['total_pixels'] / df['duration']
        df['fsr_acres_per_day'] = df['total_pixels'] / df['duration']
        df['fsr_ha_per_day'] = df['total_pixels'] / df['duration']
        df['max_growth_pixels'] = group['pixels'].transform('max')
        df['min_growth_pixels'] = group['pixels'].transform('min')
        df['mean_growth_pixels'] = group['pixels'].transform('mean')
        df['fsr_km2_per_day'] = df['fsr_km2_per_day'].apply(toKms, res=res)
        df['fsr_acres_per_day'] = df['fsr_acres_per_day'].apply(toAcres,
                                                                res=res)
        df['fsr_ha_per_day'] = df['fsr_ha_per_day'].apply(toHa, res=res)
        df['max_growth_km2'] = df['max_growth_pixels'].apply(toKms, res=res)
        df['max_growth_acres'] = df['max_growth_pixels'].apply(toAcres,
                                                               res=res)
        df['max_growth_ha'] = df['max_growth_pixels'].apply(toHa, res=res)
        df['min_growth_km2'] = df['min_growth_pixels'].apply(toKms, res=res)
        df['min_growth_acres'] = df['min_growth_pixels'].apply(toAcres,
                                                               res=res)
        df['min_growth_ha'] = df['min_growth_pixels'].apply(toHa, res=res)
        df['mean_growth_km2'] = df['mean_growth_pixels'].apply(toKms, res=res)
        df['mean_growth_acres'] = df['mean_growth_pixels'].apply(toAcres,
                                                                 res=res)
        df['mean_growth_ha'] = df['mean_growth_pixels'].apply(toHa, res=res)
        df['date'] = df['date'].apply(lambda x: x.strftime('%Y-%m-%d'))
        df = df[['id', 'x', 'y', 'total_pixels', 'date', 'ignition_date',
                 'ignition_day', 'ignition_month', 'ignition_year',
                 'last_date', 'duration', 'total_area_km2', 'total_area_acres',
                 'total_area_ha', 'fsr_pixels_per_day', 'fsr_km2_per_day',
                 'fsr_acres_per_day', 'fsr_ha_per_day', 'max_growth_pixels',
                 'min_growth_pixels', 'mean_growth_pixels', 'max_growth_km2',
                 'max_growth_acres', 'max_growth_ha', 'min_growth_km2',
                 'min_growth_acres', 'min_growth_ha', 'mean_growth_km2',
                 'mean_growth_acres', 'mean_growth_ha']]
        df = df.drop_duplicates()
        df.index = df['id']
        df['max_growth_rates'] = max_rate_dates

        # Attach names to landcover and ecoregion codes if requested
        if self.landcover:
            print('Adding landcover attributes...')

            # Get mosaicked landcover geotiffs
            lc_files = glob(os.path.join(lc_dir, "*tif"))
            lc_files.sort()
            lc_years = [f[-8:-4] for f in lc_files]
            lc_files = {lc_years[i]: lc_files[i] for i in range(len(lc_files))}

            # We have a different landcover file for each year (almost)
            df['year'] = df['date'].apply(lambda x: x[:4])

            # Rasterio point querier
            def pointQuery(row):
                x = row['x']
                y = row['y']
                val = int([val for val in lc.sample([(x, y)])][0])
                return val

            # This works faster if split by year
            sdfs = []
            for year in tqdm(df['year'].unique(), position=0):
                sdf = df[df['year'] == year]
                if year > max(lc_years):
                    year = max(lc_years)
                lc = rasterio.open(lc_files[year])
                sdf['landcover'] = sdf.apply(pointQuery, axis=1)
                sdfs.append(sdf)
            df = pd.concat(sdfs)

        if self.ecoregion:
            pass
            # ...

        # Save event level attributes
        print("Saving data frame to 'event_attributes.csv'...")
        df.to_csv('data/tables/event_attributes.csv', index=False)

    def buildPoints(self):
       # Read in the event table
        print("Reading classified fire event table...")
        df = pd.read_csv(self.dest)
    
        # Go ahead and create daily id (did) for later
        df["did"] = df["id"].astype(str) + "-" + df["date"]
    
        # Get geometries
        crs = self.crs
        geom = self.geom
        proj4 = crs.proj4
        resolutions = [geom[1], geom[-1]]
    
        # Filter columns, center pixel coordinates, and remove repeating pixels
        df = df[["id", "did", "date", "x", "y"]]
        df["x"] = df["x"] + (resolutions[0]/2)
        df["y"] = df["y"] + (resolutions[1]/2)
    
        # Each entry gets a point object from the x and y coordinates.
        print("Converting data frame to spatial object...")
        df["geometry"] = df[["x", "y"]].apply(lambda x: Point(tuple(x)),
                                              axis=1)
        gdf = df[["id", "did", "date", "geometry"]]
        gdf = gpd.GeoDataFrame(gdf, crs=proj4, geometry=gdf["geometry"])

        return gdf

    def buildPolygons(self, daily_shp_path, event_shp_path):
        # Make sure we have the target folders
        if not(os.path.exists(os.path.dirname(event_shp_path))):
            os.makedirs(os.path.dirname(event_shp_path))
    
        # Create a spatial points object
        gdf = self.buildPoints()
        
        # Create a circle buffer
        print("Creating buffer...")
        geometry = gdf.buffer(1 + (self.res/2))
        gdf["geometry"] = geometry
    
        # Then create a square envelope around the circle
        gdf["geometry"] = gdf.envelope
    
        # Now add the first date of each event and merge daily event detections
        print("Dissolving polygons...")
        gdf["start_date"] = gdf.groupby("id")["date"].transform("min")
        gdfd = gdf.dissolve(by="did", as_index=False)
        gdfd["year"] = gdfd["start_date"].apply(lambda x: x[:4])
        gdfd["month"] = gdfd["start_date"].apply(lambda x: x[5:7])
    
        # Save the daily before dissolving into event level
        print("Saving daily file to " + daily_shp_path + "...")
        gdfd.to_file(daily_shp_path, driver="GPKG")
    
        # Now merge into event level polygons
        gdf = gdf[["id", "start_date", "geometry"]]
        gdf = gdf.dissolve(by="id", as_index=False)
    
        # For each geometry, if it is a single polygon, cast as a multipolygon
        print("Converting polygons to multipolygons...")
        def asMultiPolygon(polygon):
            if type(polygon) == Polygon:
                polygon = MultiPolygon([polygon])
            return polygon
        gdf["geometry"] = gdf["geometry"].apply(asMultiPolygon)
    
        # Calculate perimeter length
        print("Calculating perimeter lengths...")
        gdf["final_perimeter"] = gdf["geometry"].length
    
        # Now save as a geopackage
        print("Saving event-level file to " + event_shp_path + "...")
        gdf.to_file(event_shp_path, driver="GPKG")
    

def convertDates(array, year):
    """
    Convert everyday in an array to days since Jan 1 1970
    """
    def convertDate(julien_day, year):
        base = dt.datetime(1970, 1, 1)
        date = dt.datetime(year, 1, 1) + dt.timedelta(int(julien_day))
        days = date - base
        return days.days

    # Loop through each position with data and convert
    locs = np.where(array > 0)
    ys = locs[0]
    xs = locs[1]
    locs = [[ys[i], xs[i]] for i in range(len(xs))]
    for l in locs:
        y = l[0]
        x = l[1]
        array[y, x] = convertDate(array[y, x], year)

    return array


def dateRange(perimeter):
    """
    Converts days in a perimeter object since Jan 1 1970 to date strings
    """
    if len(perimeter.coords) > 0:
        base = dt.datetime(1970, 1, 1)
        days = [p[2] for p in perimeter.coords]
        day1 = (base + dt.timedelta(days=int(min(days)))).strftime("%Y-%m-%d")
    else:
        day1 = "N/A"
    return day1


def edgeCheck(yedges, xedges, coord, sp_buffer):
    """
    Identify edge cases to make merging events quicker later
    """
    y = coord[0]
    x = coord[1]
    if y in yedges:
        edge = True
    elif x in xedges:
        edge = True
    else:
        edge = False
    return edge


def flttn(lst):
    """
    Just a quick way to flatten lists of lists
    """
    lst = [l for sl in lst for l in sl]
    return lst


def maxGrowthDate(x):
    dates = x["date"].to_numpy()
    pixels = x["pixels"].to_numpy()
    loc = np.where(pixels == np.max(pixels))[0]
    d = np.unique(dates[loc])
    if len(d) > 1:
        d = ", ".join(d)
    else:
        d = d[0]
    return d


def mergeChecker(new_coords, full_list, temporal_param, radius):
    """
    This uses a radius for the spatial window as opposed to a square and is not
    currently being used to merge events.
    """
    t1 = np.min([c[2] for c in new_coords]) - temporal_param
    t2 = np.max([c[2] for c in new_coords]) + temporal_param
    for i in range(len(full_list)):
        old_event = full_list[i]
        old_coords = old_event[1]
        old_times = [c[2] for c in old_coords]
        time_checks = [t for t in old_times if t >= t1 and t <= t2]

        if len(time_checks) > 0:
            for coord in new_coords:
                # Check if the time coordinate is within an old event
                radii = []
                new_y = coord[0]
                new_x = coord[1]
                for oc in old_coords:
                    old_y = oc[0]
                    old_x = oc[1]
                    dy = abs(old_y - new_y)
                    dx = abs(old_x - new_x)
                    r = np.sqrt((dy ** 2) + (dx ** 2))
                    radii.append(r)
                check = [r for r in radii if r <= radius]
                if any(check):
                    return i, True
                else:
                    return i, False
            else:
                return i, False


def mode(lst):
    if len(np.unique(lst)) > 1:
        grouped_lst = [list(lst[lst == s]) for s in lst]
        counts = {len(a): a for a in grouped_lst}  # overwrites matches
        max_count = np.max(list(counts.keys()))
        mode = counts[max_count][0]
    else:
        mode = lst.unique()[0]
    return mode


def pquery(p, lc, lc_array):
    """
    Find the landcover code for a particular point (p).
    """
    row, col = lc.index(p.x, p.y)
    lc_value = lc_array[row, col]
    return lc_value


def rasterize(src, dst, attribute, resolution, crs, extent, all_touch=False,
              na=-9999):

    # Open shapefile, retrieve the layer
    src_data = ogr.Open(src)
    layer = src_data.GetLayer()

    # Use transform to derive coordinates and dimensions
    xmin = extent[0]
    ymin = extent[1]
    xmax = extent[2]
    ymax = extent[3]

    # Create the target raster layer
    cols = int((xmax - xmin)/resolution)
    rows = int((ymax - ymin)/resolution) + 1
    trgt = gdal.GetDriverByName("GTiff").Create(dst, cols, rows, 1,
                                gdal.GDT_Float32)
    trgt.SetGeoTransform((xmin, resolution, 0, ymax, 0, -resolution))

    # Add crs
    refs = osr.SpatialReference()
    refs.ImportFromWkt(crs)
    trgt.SetProjection(refs.ExportToWkt())

    # Set no value
    band = trgt.GetRasterBand(1)
    band.SetNoDataValue(na)

    # Set options
    if all_touch is True:
        ops = ["-at", "ATTRIBUTE=" + attribute]
    else:
        ops = ["ATTRIBUTE=" + attribute]

    # Finally rasterize
    gdal.RasterizeLayer(trgt, [1], layer, options=ops)

    # Close target an source rasters
    del trgt
    del src_data


def spCheck(diffs, sp_buf):
    """
    Quick function to check if events land within the spatial window.
    """
    checks = [e for e in diffs if abs(e) < sp_buf]
    if any(checks):
        check = True
    else:
        check = False
    return check

def toAcres(p, res):
    return (p*res**2) * 0.000247105


def toDays(date, base):
    """
    Convert dates to days since a base date
    """
    if type(date) is str:
        date = dt.datetime.strptime(date, "%Y-%m-%d")
        delta = (date - base)
        days = delta.days
    return days


def toHa(p, res):
    return (p*res**2) * 0.0001


def toKms(p, res):
    return (p*res**2)/1000000


def downloadBA(query):
    # Get file and target path
    hdf, hdf_path = query
    
    # Use file name to get the tile id
    tile = hdf[17:23]

    # Infer the target file path
    folder = os.path.join(hdf_path, tile)
    trgt = os.path.join(folder, hdf)

    # If this file doesn't exists locally, download
    if not os.path.exists(trgt):

        # Check worker into site
        ftp = ftplib.FTP("fuoco.geog.umd.edu", user="fire", passwd="burnt")

        # Infer and move into the remote folder
        ftp_folder =  "/MCD64A1/C6/" + tile
        ftp.cwd(ftp_folder)

        # Attempt to download
        try:
            with open(trgt, "wb") as dst:
                ftp.retrbinary("RETR %s" % hdf, dst.write, 102400)
        except ftplib.all_errors as e:
            print("FTP Transfer Error: ", e)

        # Close connection
        ftp.quit()
        ftp.close()

def downloadLC(query):
    link = query[0]
    dst = query[1]
    if not os.path.exists(dst):
        request = urllib2.Request(link)
        with open(dst, "wb") as file:
            response = urllib2.urlopen(request).read()
            file.write(response)

# Classes
class DataGetter:
    """
    Things to do/remember:
        - parallel downloads
    """
    def __init__(self, proj_dir):
        self.proj_dir = proj_dir
        self.date = dt.datetime.today().strftime("%m-%d-%Y")
        self.createPaths()
        self.cpus = os.cpu_count()
        self.modis_template_path = os.path.join(proj_dir, "rasters/")
        self.modis_template_file_root = "mosaic_template.tif"
        self.landcover_path = os.path.join(proj_dir, "rasters/landcover")
        self.landcover_mosaic_path = os.path.join(proj_dir,
                                                  "rasters/landcover/mosaics")
        self.landcover_file_root = "lc_mosaic_"
        self.nc_path = os.path.join(proj_dir, "rasters/burn_area/netcdfs")
        self.hdf_path = os.path.join(proj_dir, "rasters/burn_area/hdfs")
        self.tiles = ["h08v04", "h09v04", "h10v04", "h11v04", "h12v04",
                      "h13v04", "h08v05", "h09v05", "h10v05", "h11v05",
                      "h12v05", "h08v06", "h09v06", "h10v06", "h11v06"]
        print("Project Folder: " + proj_dir)

    def createPaths(self):
        sub_folders = ["rasters/burn_area", "rasters/burn_area/hdfs",
                       "rasters/ecoregion", "rasters/landcover",
                       "rasters/landcover/mosaics/", "shapefiles/ecoregion",
                       "tables"]
        folders = [os.path.join(self.proj_dir, sf) for sf in sub_folders]
        for f in folders:
            if not os.path.exists(f):
                os.makedirs(f)

    def getBurns(self):
        """
        This will download the MODIS burn event data set tiles and create a
        singular mosaic to use as a template file for coordinate reference
        information and geometries.

        User manual:
            http://modis-fire.umd.edu/files/MODIS_C6_BA_User_Guide_1.2.pdf

        FTP:
            ftp://fire:burnt@fuoco.geog.umd.edu/gfed4/MCD64A1/C6/
        """
        # Check in to the site
        ftp = ftplib.FTP("fuoco.geog.umd.edu", user="fire", passwd="burnt")
        ftp.cwd("MCD64A1/C6")

        # Use specified tiles or...download all tiles if the list is empty
        if self.tiles[0].lower() != "all":
            tiles = self.tiles
        else:
            tiles = ftp.nlst()
            tiles = [t for t in tiles if "h" in t]

        # Download the available files and catch failed downloads
        for tile in tiles:
            # Find remote folder
            ftp_folder =  "/MCD64A1/C6/" + tile
            ftp.cwd(ftp_folder)
            hdfs = ftp.nlst()
            hdfs = [h for h in hdfs if ".hdf" in h]

            # Make sure local target folder exists
            folder = os.path.join(self.hdf_path, tile)
            if not os.path.exists(folder):
                os.mkdir(folder)

            # Skip this if the final product exists
            nc_file = os.path.join(
                    self.proj_dir, "rasters/burn_area/netcdfs/" + tile + ".nc")
            if not os.path.exists(nc_file):
                print("Downloading/Checking hdf files for " + tile)

                # Create pool
                pool = Pool(4)

                # Zip arguments together
                queries = list(zip(hdfs, np.repeat(self.hdf_path, len(hdfs))))

                # Try to dl in parallel with progress bar
                try:
                    for _ in tqdm(pool.imap(downloadBA, queries),
                                  total=len(hdfs),  position=0):
                        pass
                except ftplib.error_temp:
                    print("Too many connections from this IP attempted. Try " +
                          "again later.")
                except:
                    try:
                        _ = [downloadBA(q) for q in tqdm(queries, position=0)]
                    except Exception as e:
                        template = "\nDownload failed: error type {0}:\n{1!r}"
                        message = template.format(type(e).__name__, e.args)
                        print(message)

            # Check Downloads
            missings = []
            for hdf in hdfs:
                trgt = os.path.join(folder, hdf)
                remote = os.path.join(ftp_folder, hdf)
                if not os.path.exists(trgt):
                    missings.append(remote)
                else:
                    try:
                        gdal.Open(trgt).GetSubDatasets()[0][0]
                    except:
                        print("Bad file detected, removing to try again...")
                        missings.append(remote)
                        os.remove(trgt)

            # Now try again for the missed files
            if len(missings) > 0:
                print("Missed Files: \n" + str(missings))
                print("trying again...")

                # Check into FTP server again
                ftp = ftplib.FTP("fuoco.geog.umd.edu", user="fire",
                                 passwd="burnt")
                for remote in missings:
                    tile = remote.split("/")[-2]
                    ftp_folder =  "/MCD64A1/C6/" + tile
                    ftp.cwd(ftp_folder)
                    file = os.path.basename(remote)
                    trgt = os.path.join(self.hdf_path, tile, file)

                    # Try to redownload
                    try:
                        with open(trgt, "wb") as dst:
                            ftp.retrbinary("RETR %s" % file, dst.write, 102400)
                    except ftplib.all_errors as e:
                        print("FTP Transfer Error: ", e)

                    # Check download
                    try:
                        gdal.Open(trgt).GetSubDatasets()[0][0]
                        missings.remove(file)
                    except Exception as e:
                        print(e)

                # Close new ftp connection
                ftp.quit()
                ftp.close()

                # If that doesn"t get them all, give up.
                if len(missings) > 0:
                    print("There are still " + str(len(missings)) +
                          " missed files.")
                    print("Try downloading these files manually: ")
                    for m in missings:
                        print(m)

        # Build the netcdfs here
        tile_files = {}
        for tid in tiles:
            files = glob(os.path.join(self.hdf_path, tid, "*hdf"))
            tile_files[tid] = files

        # Merge one year into a reference mosaic
        if not os.path.exists(self.modis_template_path):
            folders = glob(os.path.join(self.hdf_path, "*"))
            file_groups = [glob(os.path.join(f, "*hdf")) for f in folders]
            for f in file_groups:
                f.sort()
            files = [f[0] for f in file_groups]
            dss = [rasterio.open(f).subdatasets[0] for f in files]
            tiles = [rasterio.open(d) for d in dss]
            mosaic, transform = merge(tiles)
            crs = tiles[0].meta.copy()
            template_path = os.path.join(self.modis_template_path,
                                        self.modis_template_file_root)
            crs.update({"driver": "GTIFF",
                       "height": mosaic.shape[1],
                       "width": mosaic.shape[2],
                       "transform": transform})
            with rasterio.open(template_path, "w", **crs) as dest:
                dest.write(mosaic)

        # Build one netcdf per tile
        for tid in tiles:
            files = tile_files[tid]
            if len(files) > 0:
                try:
                    self.buildNCs(files)
                except Exception as e:
                    file_name = os.path.join(self.nc_path, tid + ".nc")
                    print("Error on tile " + tid + ": " + str(e))
                    print("Removing " + file_name + " and moving on.")
                    os.remove(file_name)

    def getLandcover(self, landcover=1):
        """
        A method to download and process landcover data from NASA"s Land
        Processes Distributed Active Archive Center, which is an Earthdata
        thing. You"ll need register for a username and password, but that"s
        free. Fortunately, there is a tutorial on how to get this data:

        https://wiki.earthdata.nasa.gov/display/EL/How+To+Access+Data+With+
        Python

        sample citation for later:
           ASTER Mount Gariwang image from 2018 was retrieved from
           https://lpdaac.usgs.gov, maintained by the NASA EOSDIS Land
           Processes Distributed Active Archive Center (LP DAAC) at the USGS
           Earth Resources Observation and Science (EROS) Center, Sioux Falls,
           South Dakota. 2018, https://lpdaac.usgs.gov/resources/data-action/
           aster-ultimate-2018-winter-olympics-observer/.
        """
        # Use specified tiles or...
        if self.tiles[0].lower() != "all":
            tiles = self.tiles

        # ...download all tiles if the list is empty
        else:
            # Check in to the burn data site
            ftp = ftplib.FTP("fuoco.geog.umd.edu")
            ftp.login("fire", "burnt")
            ftp.cwd("/MCD64A1/C6/")
            tiles = ftp.nlst()
            tiles = [t for t in tiles if "h" in t]

        # Get the full string for land cover type
        lc_type = "type" + str(landcover)

        # Access
        print("Retrieving land cover rasters from NASA's Earthdata service...")
        print("Register at the link below to obtain a username and password:")
        print("https://urs.earthdata.nasa.gov/")
        username = input("Enter NASA Earthdata User Name: ")
        password = input("Enter NASA Earthdata Password: ")
        pw_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
        pw_manager.add_password(None, "https://urs.earthdata.nasa.gov",
                               username, password)
        cookiejar = CookieJar()
        opener = urllib2.build_opener(urllib2.HTTPBasicAuthHandler(pw_manager),
                                     urllib2.HTTPCookieProcessor(cookiejar))
        urllib2.install_opener(opener)

        # Get available years
        url = "https://e4ftl01.cr.usgs.gov/MOTA/MCD12Q1.006/"
        r = requests.get(url)
        soup = BeautifulSoup(r.text, 'html.parser')
        links = [link["href"] for link in soup.find_all("a", href=True)]
        years = [l.split(".")[0] for l in links if "01.01" in l]

        # Land cover data from earthdata.nasa.gov
        lp = self.landcover_path
        for y in years:
            print("\nRetrieving landcover data for " + y )

            # Make sure destination folder exists            
            if not os.path.exists(os.path.join(lp, y)):
                    os.mkdir(os.path.join(lp, y))

            # Retrieve list of links to hdf files
            url = ("https://e4ftl01.cr.usgs.gov/MOTA/MCD12Q1.006/" + y +
                  ".01.01/")
            r = requests.get(url)
            soup = BeautifulSoup(r.text, 'html.parser')
            names = [link["href"] for link in soup.find_all("a", href=True)]
            names = [n for n in names if "hdf" in n and "xml" not in n]
            names = [n for n in names if n.split('.')[2] in tiles]
            links = [url + l for l in names]

            # Build list of target local file paths and check if they're needed
            dsts = [os.path.join(lp, y, names[i]) for i in range(len(links))]
            dsts = [dst for dst in dsts if not(os.path.exists(dst))]

            # Group links and local paths for parallel downloads  # <---------- Attempt to skip when files are present breaks here
            queries = [(links[i], dsts[i]) for i in range(len(links))]

            # Use the number of physical cores
            ncores = cpu_count()
            pool = Pool(int(ncores /2))
            try:
                for _ in tqdm(pool.imap(downloadLC, queries),
                              total=len(queries), position=0):
                    pass
            except:
                print("\nError, attempting serial download...")
                try:
                    _ = [downloadLC(q) for q in tqdm(queries, position=0)]
                except Exception as e:
                    template = "\nDownload failed: error type {0}:\n{1!r}"
                    message = template.format(type(e).__name__, e.args)
                    print(message)

        # Now process these tiles into yearly geotiffs.
        if not os.path.exists(self.landcover_mosaic_path):
            os.mkdir(self.landcover_mosaic_path)
        for y in years:
            print("\nMosaicking landcover tiles for year " + y)

            # Filter available files for the requested tiles
            lc_files = glob(os.path.join(self.landcover_path, y, "*hdf"))
            lc_files = [f for f in lc_files if f.split(".")[2] in self.tiles]

            # Use the subdataset name to get the right land cover type
            data_sets = []
            for f in lc_files:
                subdss = rasterio.open(f).subdatasets
                trgt_ds = [sd for sd in subdss if lc_type in sd.lower()][0]
                data_sets.append(trgt_ds)

            # Create pointers to the chosen land cover type
            tiles = [rasterio.open(ds) for ds in data_sets]

            # Mosaic them together
            mosaic, transform = merge(tiles)

            # Get coordinate reference information
            crs = tiles[0].meta.copy()
            crs.update({"driver": "GTIFF",
                        "height": mosaic.shape[1],
                        "width": mosaic.shape[2],
                        "transform": transform})

            # Save mosaic file
            file = self.landcover_file_root + lc_type + "_" + y + ".tif"
            path = os.path.join(self.landcover_mosaic_path, file)
            with rasterio.open(path, "w", **crs) as dest:
                dest.write(mosaic)

        # Print location
        print("\nLandcover data saved to " + self.landcover_mosaic_path)


    def getShapes(self):
        """
        Just to grab some basic shapefiles needed for calculating statistics.
        """
        if not os.path.exists(os.path.join(self.proj_dir, "shapefiles")):
            os.mkdir(os.path.join(self.proj_dir, "shapefiles"))

        # Variables
        conus_states = ["WV", "FL", "IL", "MN", "MD", "RI", "ID", "NH", "NC",
                        "VT", "CT", "DE", "NM", "CA", "NJ", "WI", "OR", "NE",
                        "PA", "WA", "LA", "GA", "AL", "UT", "OH", "TX", "CO",
                        "SC", "OK", "TN", "WY", "ND", "KY", "VI", "ME", "NY",
                        "NV", "MI", "AR", "MS", "MO", "MT", "KS", "IN", "SD",
                        "MA", "VA", "DC", "IA", "AZ"]
        modis_crs = ("+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +a=6371007.181 " +
                     "+b=6371007.181 +units=m +no_defs")

        # MODIS Sinusoial World Grid
        if not os.path.exists(
                os.path.join(self.proj_dir,
                             "shapefiles/modis_world_grid.shp")):
            print("Downloading MODIS Sinusoidal Projection Grid...")
            src = ("http://book.ecosens.org/wp-content/uploads/2016/06/" +
                   "modis_grid.zip")
            modis = gpd.read_file(src)
            modis.crs = modis_crs
            modis.to_file(os.path.join(self.proj_dir,
                                       "shapefiles/modis_world_grid.shp"))

        # Contiguous United States - WGS84
        if not os.path.exists(os.path.join(self.proj_dir,
                                           "shapefiles/conus.shp")):
            print("Downloading US state shapefile from the Census Bureau...")
            usa = gpd.read_file("http://www2.census.gov/geo/tiger/GENZ2016/" +
                                "shp/cb_2016_us_state_20m.zip")
            conus = usa[usa["STUSPS"].isin(conus_states)]
            conus.crs = {"init": "epsg:4326", "no_defs": True}
            conus.to_file(os.path.join(self.proj_dir, "shapefiles/conus.shp"))

        # Contiguous United States - MODIS Sinusoidal
        if not os.path.exists(os.path.join(self.proj_dir,
                                           "shapefiles/conus_modis.shp")):
            print("Reprojecting state shapefile to MODIS Sinusoidal...")
            conus = gpd.read_file(os.path.join(self.proj_dir,
                                               "shapefiles/conus.shp"))
            modis_conus = conus.to_crs(modis_crs)
            modis_conus.to_file(os.path.join(self.proj_dir,
                                             "shapefiles/conus_modis.shp"))

        # Level III Omernick Ecoregions - USGS North American Albers
        if not os.path.exists(
                os.path.join(self.proj_dir,
                             "shapefiles/ecoregion/us_eco_l3.shp")):
            print("Downloading Omernick Level III Ecoregions from the USGS...")
            eco_l3 = gpd.read_file("ftp://ftp.epa.gov/wed/ecoregions/us/" +
                                   "us_eco_l3.zip")
            eco_l3.crs = {"init": "epsg:5070"}
            eco_l3.to_file(os.path.join(self.proj_dir,
                                        "shapefiles/ecoregion/us_eco_l3.shp"))
            eco_l3 = eco_l3.to_crs(modis_crs)
            eco_l3.to_file(
                    os.path.join(self.proj_dir,
                                 "shapefiles/ecoregion/us_eco_l3_modis.shp"))
            eco_ref = eco_l3[["US_L3CODE", "NA_L3NAME", "NA_L2NAME",
                              "NA_L1NAME"]].drop_duplicates()
            def cap(string):
                strings = string.split()
                strings = [s.lower() if s != "USA" else s for s in strings]
                caps = [s.capitalize()  if s != "USA" else s for s in strings]
                return " ".join(caps)
            eco_ref["NA_L2NAME"] = eco_ref["NA_L2NAME"].apply(cap)
            eco_ref["NA_L1NAME"] = eco_ref["NA_L1NAME"].apply(cap)
            eco_ref.to_csv(os.path.join(self.proj_dir, "tables/eco_refs.csv"),
                           index=False)

        # Rasterize Level Omernick Ecoregions - WGS 84
        if not os.path.exists(
                os.path.join(self.proj_dir,
                             "rasters/ecoregion/us_eco_l3_modis.tif")):

            # We need something with the correct geometry
            src = os.path.join(self.proj_dir,
                               "shapefiles/ecoregion/us_eco_l3_modis.shp")
            dst = os.path.join(self.proj_dir,
                               "rasters/ecoregion/us_eco_l3_modis.tif")
            extent_template_file = os.path.join(
                    self.proj_dir, "shapefiles/modis_world_grid.shp")

            # Getting the extent regardless of existing files from other runs
            template1 = gpd.read_file(extent_template_file)
            template1["h"] = template1["h"].apply(lambda x: "{:02d}".format(x))
            template1["v"] = template1["v"].apply(lambda x: "{:02d}".format(x))
            template1["tile"] = "h" + template1["h"] + "v" +  template1["v"]
            template1 = template1[template1["tile"].isin(self.tiles)]

            # We can use this to query which tiles are needed for coordinates
            bounds = template1.geometry.bounds
            minx = min(bounds["minx"])
            miny = min(bounds["miny"])
            maxx = max(bounds["maxx"])
            maxy = max(bounds["maxy"])
            minx_tile = template1["tile"][bounds["minx"] == minx].iloc[0]
            miny_tile = template1["tile"][bounds["miny"] == miny].iloc[0]
            maxx_tile = template1["tile"][bounds["maxx"] == maxx].iloc[0]
            maxy_tile = template1["tile"][bounds["maxy"] == maxy].iloc[0]
            extent_tiles = [minx_tile, miny_tile, maxx_tile, maxy_tile]

            # If these aren"t present, I say just go ahead and download
            exts = []
            for et in extent_tiles:
                folder = os.path.join(self.proj_dir, "rasters/burn_area/hdfs",
                                      et)
                if not os.path.exists(folder):
                    self.getBurns()
                file = glob(os.path.join(folder, "*hdf"))[0]
                file_pointer = gdal.Open(file)
                dataset_pointer = file_pointer.GetSubDatasets()[0][0]
                ds = gdal.Open(dataset_pointer)
                geom = ds.GetGeoTransform()
                ulx, xres, xskew, uly, yskew, yres = geom
                lrx = ulx + (ds.RasterXSize * xres)
                lry = uly + (ds.RasterYSize * yres) + yres
                exts.append([ulx, lry, lrx, uly])

            extent = [exts[0][0], exts[1][1], exts[2][2], exts[3][3]]
            wkt = ds.GetProjection()
            attribute = "US_L3CODE"
            rasterize(src, dst, attribute, xres, wkt, extent)

    def shapeToTiles(self, shp_path):
        """
        Set or reset the tile list using a shapefile. Where shapes intersect
        with the modis sinusoidal grid determines which tiles to use.
        """
        source = gpd.read_file(shp_path)

        modis_crs = {'proj': 'sinu', 'lon_0': 0, 'x_0': 0, 'y_0': 0,
                     'a': 6371007.181, 'b': 6371007.181, 'units': 'm',
                     'no_defs': True}

        # Attempt to project to modis sinusoidal
        try:
            source = source.to_crs(modis_crs)
        except Exception as e:
            print("Error: " + str(e))
            print("Failed to reproject file, ensure a coordinate reference " +
                  "system is specified.")

        # Attempt to read in the modis grid and download it if not available
        try:
            modis_grid = gpd.read_file(
                    os.path.join(self.proj_dir,
                                 "shapefiles/modis_world_grid.shp"))
        except:
            modis_grid = gpd.read_file("http://book.ecosens.org/wp-content/" +
                                       "uploads/2016/06/modis_grid.zip")
            modis_grid.to_file(os.path.join(self.proj_dir,
                                            "shapefiles/modis_world_grid.shp"))

        # Left join shapefiles with source shape as the left
        shared = gpd.sjoin(source, modis_grid, how="left").dropna()
        shared["h"] = shared["h"].apply(lambda x: "h{:02d}".format(int(x)))
        shared["v"] = shared["v"].apply(lambda x: "v{:02d}".format(int(x)))
        shared["tile"] = shared["h"] + shared["v"]
        tiles = pd.unique(shared["tile"].values)
        self.tiles = tiles


    def buildNCs(self, files):
        """
        Take in a time series of files for the MODIS burn detection dataset and
        create a singular netcdf file.
        """
        savepath = self.nc_path

        # Check that the target folder exists, agian.
        if not os.path.exists(savepath):
            os.mkdir(savepath)

        # Set file names
        files.sort()
        names = [os.path.split(f)[-1] for f in files]
        names = [f.split(".")[2] + "_" + f.split(".")[1][1:] for f in names]
        tile_id = names[0].split("_")[0]
        file_name = os.path.join(savepath, tile_id + ".nc")

        # Skip if it exists already
        if os.path.exists(file_name):
            print(tile_id + " netCDF file exists, skipping...")
        else:
            # Use a sample to get geography information and geometries
            print("Building netcdf for tile " + tile_id)
            sample = files[0]
            ds = gdal.Open(sample).GetSubDatasets()[0][0]
            hdf = gdal.Open(ds)
            geom = hdf.GetGeoTransform()
            proj = hdf.GetProjection()
            data = hdf.GetRasterBand(1)
            crs = osr.SpatialReference()

            # Get the proj4 string usign the WKT
            crs.ImportFromWkt(proj)
            proj4 = crs.ExportToProj4()

            # Use one tif (one array) for spatial attributes
            array = data.ReadAsArray()
            ny, nx = array.shape
            xs = np.arange(nx) * geom[1] + geom[0]
            ys = np.arange(ny) * geom[5] + geom[3]

            # Todays date for attributes
            todays_date = dt.datetime.today()
            today = np.datetime64(todays_date)

            # Create Dataset
            nco = Dataset(file_name, mode="w", format="NETCDF4", clobber=True)

            # Dimensions
            nco.createDimension("y", ny)
            nco.createDimension("x", nx)
            nco.createDimension("time", None)

            # Variables
            y = nco.createVariable("y",  np.float64, ("y",))
            x = nco.createVariable("x",  np.float64, ("x",))
            times = nco.createVariable("time", np.int64, ("time",))
            variable = nco.createVariable("value",np.int16,
                                          ("time", "y", "x"),
                                          fill_value=-9999, zlib=True)
            variable.standard_name = "day"
            variable.long_name = "Burn Days"

            # Appending the CRS information
            # Check "https://cf-trac.llnl.gov/trac/ticket/77"
            crs = nco.createVariable("crs", "c")
            variable.setncattr("grid_mapping", "crs")
            crs.spatial_ref = proj4
            crs.proj4 = proj4
            crs.geo_transform = geom
            crs.grid_mapping_name = "sinusoidal"
            crs.false_easting = 0.0
            crs.false_northing = 0.0
            crs.longitude_of_central_meridian = 0.0
            crs.longitude_of_prime_meridian = 0.0
            crs.semi_major_axis = 6371007.181
            crs.inverse_flattening = 0.0

            # Coordinate attributes
            x.standard_name = "projection_x_coordinate"
            x.long_name = "x coordinate of projection"
            x.units = "m"
            y.standard_name = "projection_y_coordinate"
            y.long_name = "y coordinate of projection"
            y.units = "m"

            # Other attributes
            nco.title = "Burn Days"
            nco.subtitle = "Burn Days Detection by MODIS since 1970."
            nco.description = "The day that a fire is detected."
            nco.date = pd.to_datetime(str(today)).strftime("%Y-%m-%d")
            nco.projection = "MODIS Sinusoidal"
            nco.Conventions = "CF-1.6"

            # Variable Attrs
            times.units = "days since 1970-01-01"
            times.standard_name = "time"
            times.calendar = "gregorian"
            datestrings = [f[-7:] for f in names]
            dates = []
            for d in datestrings:
                year = dt.datetime(year=int(d[:4]), month=1, day=1)
                date = year + dt.timedelta(int(d[4:]))
                dates.append(date)
            deltas = [d - dt.datetime(1970, 1, 1) for d in dates]
            days = np.array([d.days for d in deltas])

            # Write dimension data
            x[:] = xs
            y[:] = ys
            times[:] = days

            # One file a time, write the arrays
            tidx = 0
            for f in tqdm(files, position=0, file=sys.stdout):
                ds = gdal.Open(f).GetSubDatasets()[0][0]
                hdf = gdal.Open(ds)
                data = hdf.GetRasterBand(1)
                array = data.ReadAsArray()
                year = int(f[-36: -32])
                array = convertDates(array, year)
                try:
                    variable[tidx, :, :] = array
                except:
                    print(f + ": failed, probably had wrong dimensions, " +
                          "inserting a blank array in its place.")
                    blank = np.zeros((ny, nx))
                    variable[tidx, :, :] = blank
                tidx += 1

            # Done
            nco.close()


class EventPerimeter:
    def __init__(self, event_id, coord_list=[]):
        self.event_id = event_id
        self.merge_id = np.nan
        self.coords = []
        self.coords = self.add_coordinates(coord_list)

    def add_coordinates(self,coord_list):
        for coord in coord_list:
            self.coords.append(coord)
        return self.coords

    def get_event_id(self):
        return self.event_id

    def get_merge_id(self):
        return self.merge_id

    def get_coords(self):
        return self.coords


class EventGrid:
    """
    For a single file, however large, find sites with any burn detections
    in the study period, loop through these sites and group by the space-time
    window, save grouped events to a data frame on disk.
    """
    def __init__(self, proj_dir, nc_path=("rasters/burn_area/netcdfs/"),
                 spatial_param=5, temporal_param=11, area_unit="Unknown",
                 time_unit="days since 1970-01-01"):
        self.proj_dir = proj_dir
        self.nc_path = os.path.join(proj_dir, nc_path)
        self.spatial_param = spatial_param
        self.temporal_param = temporal_param
        self.area_unit = area_unit
        self.time_unit = time_unit
        self.event_grid = {}
        self.next_event_id = 1
        self.input_array = self.get_input_xarray()

    def get_input_xarray(self):
        burns = xr.open_dataset(self.nc_path)
        self.data_set = burns
        self.coordinates = burns.coords
        input_array = burns.value

        return input_array

    def add_event_grid(self, event_id, new_pts):
        for p in new_pts:
            entry = {p : event_id}
            self.event_grid.update(entry)

    def merge_perimeters(self, perimeters, event_id, obsolete_id):
        # set the merge id in the obsolete id
        perimeters[obsolete_id-1].merge_id = event_id
        new_pts = perimeters[obsolete_id-1].coords

        # update the event_grid and add points to event_id perimeter
        for p in new_pts:
            self.event_grid[p] = event_id
        perimeters[event_id-1].add_coordinates(new_pts)

        # set old perimeter to null
        merge_notice = "Merged with event {}".format(event_id)
        perimeters[obsolete_id-1].coords = [merge_notice, new_pts]

        return perimeters

    def get_spatial_window(self, y, x, array_dims):
        """
        Pull in the spatial window around a detected event and determine its
        shape and the position of the original point within it. Finding this
        origin point is related to another time saving step in the event
        classification procedure.
        """
        top = max(0, y - self.spatial_param)
        bottom = min(array_dims[0], y + self.spatial_param)
        left = max(0, x - self.spatial_param)
        right = min(array_dims[1], x + self.spatial_param)

        #  Derive full xarray coordinates from just the window for speed
        ydim = array_dims[0]
        xdim = array_dims[1]

        # Expand the spatial dimension
        tps = [i for i in range(self.spatial_param)]

        # There are four edge cases
        x_edges_1 = [0 + t for t in tps]
        x_edges_2 = [xdim - t for t in tps]
        y_edges_1 = [0 + t for t in tps]
        y_edges_2 = [ydim - t for t in tps]

        # Get the full y, x coords of the origin, and window coords of date
        if y in y_edges_1:
            ycenter = y
            oy = 0
        elif y in y_edges_2:
            ycenter = y - ydim
            oy = y - self.spatial_param
        else:
            ycenter = self.spatial_param
            oy = y - self.spatial_param
        if x in x_edges_1:
            xcenter = x
            ox = 0
        elif x in x_edges_2:
            xcenter = x - xdim
            ox = x - self.spatial_param
        else:
            xcenter = self.spatial_param
            ox = x - self.spatial_param
        center = [ycenter, xcenter]
        origin = [oy, ox]

        return top, bottom, left, right, center, origin

    def get_availables(self):
        """
        To save time, avoid checking cells with no events at any time step.
        Create a mask of max values at each point. If the maximum at a cell is
        less than or equal to zero there were no values and it will not be
        checked in the event classification step.
        """
        # Low memory - Somehow leads to slow loop in get_event_perimeters
        # We want to get the mask without pulling the whole thing into memory
#        burns = xr.open_dataset(self.nc_path, chunks={"x": 500, "y": 500})
#
#        # Pull in only the single max value array
#        mask = burns.max(dim="time").compute()
#
#        # Get the y, x positions where one or more burns were detected
#        locs = np.where(mask.value.values > 0)
#
#        # Now pair these
#        available_pairs = []
#        for i in range(len(locs[0])):
#            available_pairs.append([locs[0][i], locs[1][i]])
#
#        # Leaving the data set open causes problems
#        burns.close()

        # Using memory - can handle large tiles, but gets pretty high
        mask = self.input_array.max(dim="time")
        locs = np.where(mask > 0)
        available_pairs = []
        for i in range(len(locs[0])):
            available_pairs.append([locs[0][i], locs[1][i]])
        del mask

        # Done.
        return available_pairs

    def get_event_perimeters(self):
        """
        Iterate through each cell in the 3D MODIS Burn Date tile and group it
        into fire events using the space-time window.
        """
        print("Filtering out cells with no events...")
        available_pairs = self.get_availables()

        # If the other pointers aren"t closed first, this will be very slow
        arr = self.input_array

        # This is to check the window positions
        nz, ny, nx = arr.shape
        dims = [ny, nx]
        perimeters = []

        # traverse spatially, processing each burn day
        print("Building event perimeters...")
        for pair in tqdm(available_pairs, position=0):
            # Separate coordinates
            y, x = pair

            # get the spatial window
            [top, bottom, left,
             right, center, origin] = self.get_spatial_window(y, x, dims)
            cy, cx = center

            # what if we pull in the window?
            window = arr[:, top:bottom+1, left:right+1].data

            # The center of the window is the target burn day
            center_burn = window[:, cy, cx]
            center_burn = center_burn[center_burn > 0]

            # Loop through each event in the window and identify neighbors
            for burn in center_burn:
                new_pts = []
                curr_event_ids = []

                # Now we can get the values and position right away
                diff = abs(burn - window)
                val_locs = np.where(diff <= self.temporal_param)
                y_locs = val_locs[1]
                x_locs = val_locs[2]
                oy, ox = origin

                # Get the actual x, y positions from the window coordinates
                vals = window[val_locs]
                ys = [oy + yl for yl in y_locs]
                xs = [ox + xl for xl in x_locs]

                # Now get the geographic coordinates from tile positions
                all_ys = self.coordinates["y"].data
                all_xs = self.coordinates["x"].data
                ys = all_ys[ys]
                xs = all_xs[xs]

                # Now check if this point is in the event_grid yet
                for i in range(len(vals)):
                    curr_pt = (float(ys[i]), float(xs[i]), float(vals[i]))

                    # already assigned to an event
                    if (curr_pt in self.event_grid):
                        if self.event_grid[curr_pt] not in curr_event_ids:
                            curr_event_ids.append(self.event_grid[curr_pt])
                    else:
                        new_pts.append(curr_pt)

                # If this is a new event
                if len(curr_event_ids)==0:
                    # create a new perimeter object
                    perimeter = EventPerimeter(self.next_event_id, new_pts)

                    # append to perimeters list
                    perimeters.append(perimeter)

                    # add points to the grid
                    self.add_event_grid(self.next_event_id, new_pts)

                    # increment the event ID
                    self.next_event_id += 1

                # If all points part of same existing event
                elif len(curr_event_ids) == 1:
                    event_id = curr_event_ids[0]
                    if len(new_pts):
                        perimeters[event_id - 1].add_coordinates(new_pts)
                        self.add_event_grid(event_id, new_pts)

                # events overlap
                else:
                    perimeters = self.merge_perimeters(perimeters,
                                                       curr_event_ids[0],
                                                       curr_event_ids[1])

        return perimeters
